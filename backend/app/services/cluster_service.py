"""Kubernetes clusters (k3s for now) built from cloud-image VMs

A cluster = node VMs created with vm_service (cloud image + cloud-init) on a
ClusterNetwork (fixed MAC -> IP reservations, DNS records for the nodes and
api.<cluster>.<domain>), plus a clusters row for what only the app knows (join
token, kubeconfig, spec). Each node VM carries
<vmm:cluster name=… type=… role=… …/> metadata so the clusters table can be
rebuilt from libvirt, which stays the source of truth.

Creating, starting, stopping and scaling run as background tasks. The app talks
to the nodes only through the QEMU guest agent (guest-exec), so it needs no SSH
key and no route to the node network.
"""
import ipaddress
import logging
import random
import secrets
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from xml.sax.saxutils import quoteattr

import libvirt
import yaml
from sqlalchemy.orm import Session

from app.database import serialized
from app.events import event_bus
from app.libvirt_client import libvirt_client
from app.models import VM, CloudImage, Cluster, ClusterNode, Task
from app.schemas import ClusterCreate, TaskCreate, VMCreate
from app.services.cloud_image_service import cloud_image_service
from app.services.cluster_drivers import EL_DISTRIBUTIONS, ClusterDriver, get_driver
from app.services.cluster_network import ClusterNetwork, LibvirtClusterNetwork
from app.services.task_service import task_service
from app.services.vm_service import vm_service

logger = logging.getLogger(__name__)

METADATA_URI = "urn:vm-manager:cluster"
METADATA_KEY = "vmm"

# Preferred node images, in order (distribution, version)
IMAGE_PREFERENCE = [("debian", "13"), ("almalinux", "9"), ("debian", "12"), ("ubuntu", "24.04"),
                    ("almalinux", "10"), ("rocky", "9"), ("centos", "9-stream")]

AGENT_TIMEOUT = 15 * 60  # first boot: package updates + qemu-guest-agent install
READY_TIMEOUT = 20 * 60  # k3s download + all nodes Ready
STOP_TIMEOUT = 3 * 60

# Read-only kubectl views offered by the API (never arbitrary commands)
KUBECTL_VIEWS = {
    "nodes": ["get", "nodes", "-o", "wide"],
    "pods": ["get", "pods", "-A", "-o", "wide"],
}


class Cancelled(Exception):
    pass


def network_for(cluster: Cluster) -> ClusterNetwork:
    """The network implementation of a cluster. Lab groups will return a group-backed one here."""
    spec = cluster.spec or {}
    return LibvirtClusterNetwork(cluster.network, owned=bool(cluster.network_owned),
                                 cidr=spec.get("cidr"), zone=f"{cluster.name}.{cluster.domain}")


def _random_mac() -> str:
    return "52:54:00:%02x:%02x:%02x" % tuple(random.randint(0, 255) for _ in range(3))


class ClusterService:

    # ------------------------------------------------------------------ reading

    @serialized
    def sync_clusters(self, db: Session) -> None:
        """Re-create clusters / nodes that libvirt knows (node metadata) but the DB doesn't.
        Never deletes anything: a cluster whose VMs vanished shows its nodes as missing."""
        found: Dict[str, Dict[str, Any]] = {}
        for vm_name, xml in libvirt_client.list_vm_metadata(METADATA_URI).items():
            try:
                el = ET.fromstring(xml)
            except ET.ParseError:
                continue
            if not el.get("name"):
                continue
            entry = found.setdefault(el.get("name"), {"attrs": dict(el.attrib), "nodes": []})
            entry["nodes"].append((vm_name, dict(el.attrib)))

        changed = False
        for name, entry in found.items():
            cluster = db.query(Cluster).filter(Cluster.name == name).first()
            if cluster is None:
                attrs = entry["attrs"]
                cluster = Cluster(
                    name=name, type=attrs.get("type", "k3s"), version=attrs.get("version") or None,
                    network=attrs.get("network", "default"), network_owned=attrs.get("owned") == "yes",
                    domain=attrs.get("domain", "lab"), status="ready",
                    status_message="Rebuilt from libvirt metadata (no join token: adding workers is disabled)",
                    spec={"cidr": attrs.get("cidr")} if attrs.get("cidr") else {},
                )
                db.add(cluster)
                db.flush()
                changed = True
            known = {n.name for n in cluster.nodes}
            for vm_name, attrs in entry["nodes"]:
                if vm_name in known:
                    continue
                nics = libvirt_client.get_vm_nics(vm_name)
                cluster.nodes.append(ClusterNode(name=vm_name, role=attrs.get("role", "worker"),
                                                 ip=attrs.get("ip"), mac=nics[0]["mac"] if nics else None))
                changed = True
            if not cluster.api_ip:
                first = self._first_ctlplane(cluster)
                if first is not None:
                    cluster.api_ip = first.ip
                    changed = True
        if changed:
            db.commit()

    def list_clusters(self, db: Session) -> List[Dict[str, Any]]:
        self.sync_clusters(db)
        vm_service.sync_vms(db)
        states = {vm["name"]: vm["state"] for vm in libvirt_client.list_vms()}
        return [self.to_dict(db, c, states) for c in db.query(Cluster).order_by(Cluster.name).all()]

    def get_cluster(self, db: Session, cluster_id: int) -> Optional[Cluster]:
        return db.query(Cluster).filter(Cluster.id == cluster_id).first()

    def get_cluster_dict(self, db: Session, cluster_id: int) -> Optional[Dict[str, Any]]:
        cluster = self.get_cluster(db, cluster_id)
        if cluster is None:
            return None
        vm_service.sync_vms(db)
        return self.to_dict(db, cluster)

    def to_dict(self, db: Session, cluster: Cluster, states: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if states is None:
            states = {vm["name"]: vm["state"] for vm in libvirt_client.list_vms()}
        names = [n.name for n in cluster.nodes]
        vm_ids = {vm.name: vm.id for vm in db.query(VM).filter(VM.name.in_(names)).all()} if names else {}
        zone = f"{cluster.name}.{cluster.domain}"
        nodes = [{
            "name": n.name, "role": n.role, "ip": n.ip, "mac": n.mac, "vm_id": vm_ids.get(n.name),
            "state": states.get(n.name, "missing"), "fqdn": f"{n.name}.{zone}",
        } for n in cluster.nodes]

        status = cluster.status
        busy = task_service.is_running(cluster.task_id)
        if not busy and status in ("ready", "stopped", "starting", "stopping") and nodes:
            node_states = {n["state"] for n in nodes}
            if node_states <= {"shutoff", "missing"}:
                status = "stopped"
            elif node_states == {"running"} and status != "ready":
                status = "ready"
        task_progress = None
        if cluster.task_id:
            task = db.query(Task).filter(Task.id == cluster.task_id).first()
            task_progress = task.progress if task else None

        spec = {k: v for k, v in (cluster.spec or {}).items() if k != "password"}
        return {
            "id": cluster.id, "name": cluster.name, "type": cluster.type, "version": cluster.version,
            "network": cluster.network, "network_owned": bool(cluster.network_owned), "domain": cluster.domain,
            "api_hostname": f"api.{zone}", "api_ip": cluster.api_ip,
            "api_endpoint": f"https://{cluster.api_ip}:6443" if cluster.api_ip else None,
            "status": status, "status_message": cluster.status_message, "task_id": cluster.task_id,
            "task_running": busy, "task_progress": task_progress,
            "has_kubeconfig": bool(cluster.kubeconfig),
            "ctlplanes": sum(1 for n in cluster.nodes if n.role == "ctlplane"),
            "workers": sum(1 for n in cluster.nodes if n.role == "worker"),
            "spec": spec, "nodes": nodes, "created_at": cluster.created_at, "updated_at": cluster.updated_at,
        }

    def _publish(self, cluster: Cluster) -> None:
        event_bus.publish({"kind": "cluster", "id": cluster.id, "name": cluster.name, "status": cluster.status})

    def _set_status(self, db: Session, cluster: Cluster, status: str, message: Optional[str] = None) -> None:
        cluster.status = status
        cluster.status_message = message
        db.commit()
        self._publish(cluster)

    @staticmethod
    def _first_ctlplane(cluster: Cluster) -> Optional[ClusterNode]:
        return next((n for n in cluster.nodes if n.role == "ctlplane"), None)

    # ----------------------------------------------------------------- creating

    def _pick_image(self, db: Session, image_id: Optional[int]) -> CloudImage:
        cloud_image_service.list_images(db)  # refresh ready / missing
        ready = db.query(CloudImage).filter(CloudImage.status == "ready").all()
        if image_id is not None:
            image = next((i for i in ready if i.id == image_id), None)
            if image is None:
                raise ValueError("Cloud image not found or not downloaded yet")
            return image
        for dist, version in IMAGE_PREFERENCE:
            image = next((i for i in ready if i.distribution == dist and i.version == version), None)
            if image:
                return image
        if ready:
            return ready[0]
        raise ValueError("No cloud image is ready: download Debian 13 or AlmaLinux 9 on the Storage page")

    def create_cluster(self, db: Session, data: ClusterCreate) -> Cluster:
        get_driver(data.type)  # rejects unsupported types
        if data.ctlplanes % 2 == 0:
            raise ValueError("ctlplanes must be odd (1, 3 or 5): etcd needs a majority")
        if db.query(Cluster).filter(Cluster.name == data.name).first():
            raise ValueError(f"Cluster '{data.name}' already exists")
        for cidr in (data.pod_cidr, data.service_cidr):
            try:
                ipaddress.IPv4Network(cidr, strict=False)
            except ValueError as e:
                raise ValueError(f"Invalid CIDR {cidr}: {e}")
        if ipaddress.IPv4Network(data.pod_cidr, strict=False).overlaps(
                ipaddress.IPv4Network(data.service_cidr, strict=False)):
            raise ValueError("pod_cidr and service_cidr overlap")
        image = self._pick_image(db, data.cloud_image_id)

        existing_vms = {vm["name"] for vm in libvirt_client.list_vms()}
        clash = [n for n in self._node_names(data.name, data.ctlplanes, data.workers) if n in existing_vms]
        if clash:
            raise ValueError(f"VM '{clash[0]}' already exists")

        zone = f"{data.name}.{data.domain}"
        if data.network:
            network = LibvirtClusterNetwork.existing(data.network)
        else:
            owned_name = LibvirtClusterNetwork.owned_name(data.name)
            if any(n["name"] == owned_name for n in libvirt_client.list_networks()):
                raise ValueError(f"Network '{owned_name}' already exists")
            network = LibvirtClusterNetwork.for_new_cluster(data.name, zone, data.cidr,
                                                            avoid=[data.pod_cidr, data.service_cidr])
        node_subnet = ipaddress.IPv4Network(network.cidr) if network.owned else network.subnet()
        for label, cidr in (("pod_cidr", data.pod_cidr), ("service_cidr", data.service_cidr)):
            if ipaddress.IPv4Network(cidr, strict=False).overlaps(node_subnet):
                raise ValueError(f"{label} {cidr} overlaps the node network {node_subnet}")

        spec = data.model_dump()
        spec["cloud_image_id"] = image.id
        spec["image"] = f"{image.distribution} {image.version}"
        spec["el"] = image.distribution in EL_DISTRIBUTIONS
        spec["cidr"] = str(node_subnet)
        cluster = Cluster(
            name=data.name, type=data.type, version=data.version, network=network.name,
            network_owned=network.owned, domain=data.domain, spec=spec, token=secrets.token_hex(32),
            status="provisioning", status_message="Queued",
        )
        db.add(cluster)
        db.commit()
        db.refresh(cluster)
        self._publish(cluster)

        task = task_service.start(db, TaskCreate(
            name=f"Create {data.type} cluster {data.name}", type="cluster_create", target_type="cluster",
            target_id=cluster.id, target_name=data.name,
            description=f"{data.ctlplanes} control plane(s) + {data.workers} worker(s), {spec['image']}",
        ), self._provision, cluster.id)
        cluster.task_id = task.id
        db.commit()
        return cluster

    @staticmethod
    def _node_names(name: str, ctlplanes: int, workers: int, first_worker: int = 0) -> List[str]:
        return ([f"{name}-ctlplane-{i}" for i in range(ctlplanes)]
                + [f"{name}-worker-{i}" for i in range(first_worker, first_worker + workers)])

    def _guarded(self, db: Session, task: Task, cluster_id: int, body: Callable[[Cluster], Dict[str, Any]],
                 failed_status: str = "error") -> Dict[str, Any]:
        """Run a task body; on failure / cancel, record it on the cluster and re-raise"""
        cluster = self.get_cluster(db, cluster_id)
        if cluster is None:
            raise ValueError("Cluster was deleted")
        try:
            return body(cluster)
        except Exception as e:
            db.rollback()
            cluster = self.get_cluster(db, cluster_id)
            if cluster is not None:
                message = "Cancelled" if isinstance(e, Cancelled) else str(e)
                self._set_status(db, cluster, failed_status, message)
            raise

    def _check(self, task: Task, cluster: Cluster, db: Session, progress: Optional[int] = None,
               message: Optional[str] = None) -> None:
        if task_service.is_cancelled(task.id):
            raise Cancelled()
        if message is not None and message != cluster.status_message:
            cluster.status_message = message
            db.commit()
            self._publish(cluster)
        if progress is not None:
            task_service.update_progress(db, task.id, progress)

    def _provision(self, db: Session, task: Task, cluster_id: int) -> Dict[str, Any]:
        def body(cluster: Cluster) -> Dict[str, Any]:
            spec = cluster.spec
            self._check(task, cluster, db, 2, "Creating network")
            network = network_for(cluster)
            network.ensure()

            names = self._node_names(cluster.name, spec["ctlplanes"], spec["workers"])
            roles = ["ctlplane"] * spec["ctlplanes"] + ["worker"] * spec["workers"]
            nodes = self._add_nodes(db, task, cluster, network, list(zip(names, roles)), 5, 30)

            first = self._first_ctlplane(cluster)
            self._check(task, cluster, db, 32, f"Waiting for the guest agent of {first.name} (first boot)")
            self._wait_agent(task, first.name)
            result = self._wait_ready(db, task, cluster, 40, 92)

            self._check(task, cluster, db, 95, "Fetching kubeconfig")
            self._fetch_kubeconfig(db, cluster)
            self._set_status(db, cluster, "ready", None)
            return {**result, "api_endpoint": f"https://{cluster.api_ip}:6443", "version": cluster.version}

        return self._guarded(db, task, cluster_id, body)

    def _add_nodes(self, db: Session, task: Task, cluster: Cluster, network: ClusterNetwork,
                   nodes: List[tuple], progress_from: int, progress_to: int) -> List[ClusterNode]:
        """Reserve addresses, publish names, create and start the node VMs"""
        spec = cluster.spec
        driver = get_driver(cluster.type)
        if not cluster.token:
            raise ValueError("This cluster has no join token (rebuilt from libvirt): can't add nodes")
        zone = f"{cluster.name}.{cluster.domain}"
        api_hostname = f"api.{zone}"
        taken = [n.ip for n in cluster.nodes if n.ip]
        created: List[ClusterNode] = []
        for name, role in nodes:
            ip = network.allocate_ip(taken)
            taken.append(ip)
            node = ClusterNode(name=name, role=role, ip=ip, mac=_random_mac())
            cluster.nodes.append(node)
            created.append(node)
        first = self._first_ctlplane(cluster)
        cluster.api_ip = first.ip
        db.commit()

        ctx = {
            "name": cluster.name, "zone": zone, "api_hostname": api_hostname, "api_ip": cluster.api_ip,
            "token": cluster.token, "version": cluster.version or spec.get("version"),
            "ctlplanes": spec["ctlplanes"], "pod_cidr": spec["pod_cidr"], "service_cidr": spec["service_cidr"],
            "extra_args": spec.get("extra_args"), "el": spec.get("el"),
        }
        step = (progress_to - progress_from) / max(len(created), 1)
        for i, node in enumerate(created):
            self._check(task, cluster, db, int(progress_from + i * step), f"Creating {node.name}")
            network.reserve(node.name, node.mac, node.ip)
            hostnames = [f"{node.name}.{zone}", node.name]
            if node is first:
                hostnames += [api_hostname, f"api-int.{zone}"]
            network.publish(node.ip, hostnames)
            self._create_node_vm(db, cluster, driver, ctx, node, node is first)
        for node in sorted(created, key=lambda n: n.role != "ctlplane"):
            libvirt_client.start_vm(node.name)
        return created

    def _create_node_vm(self, db: Session, cluster: Cluster, driver: ClusterDriver, ctx: Dict[str, Any],
                        node: ClusterNode, first: bool) -> None:
        spec = cluster.spec
        res = spec["ctlplane"] if node.role == "ctlplane" else spec["worker"]
        user_data = cloud_image_service.build_user_data(
            hostname=node.name,
            username=spec.get("username"),
            password=spec.get("password"),
            ssh_keys=[k.strip() for k in spec.get("ssh_keys") or [] if k.strip()],
            custom=None,
            # console-setup (used for the layout) only exists on Debian/Ubuntu
            keyboard=None if spec.get("el") else spec.get("keyboard"),
            extra=driver.node_cloud_config(ctx, {"name": node.name, "role": node.role, "ip": node.ip,
                                                 "first": first}),
        )
        vm_service.create_vm(db, VMCreate(
            name=node.name, description=f"{cluster.type} cluster {cluster.name}: {node.role}",
            memory=res["memory"], vcpu=res["vcpu"], disk_size=res["disk_size"],
            cloud_image_id=spec["cloud_image_id"], cloudinit_userdata=user_data,
            network_name=cluster.network, mac_address=node.mac, os_type=spec.get("image"), start=False,
        ))
        attrs = {"name": cluster.name, "type": cluster.type, "role": node.role, "ip": node.ip,
                 "domain": cluster.domain, "network": cluster.network,
                 "owned": "yes" if cluster.network_owned else "no", "cidr": spec.get("cidr") or "",
                 "version": cluster.version or ""}
        xml = "<cluster " + " ".join(f"{k}={quoteattr(str(v))}" for k, v in attrs.items()) + "/>"
        libvirt_client.set_vm_metadata(node.name, METADATA_URI, METADATA_KEY, xml)

    def _sleep(self, task: Task, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            if task_service.is_cancelled(task.id):
                raise Cancelled()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(1.0, left))

    def _wait_agent(self, task: Task, vm_name: str, timeout: float = AGENT_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while not libvirt_client.guest_ping(vm_name):
            if time.monotonic() > deadline:
                raise TimeoutError(f"The QEMU guest agent of {vm_name} did not answer within "
                                   f"{timeout // 60:.0f} min (is qemu-guest-agent installed? check the console)")
            self._sleep(task, 5)

    def _exec(self, vm_name: str, argv: List[str], timeout: float = 60) -> Dict[str, Any]:
        return libvirt_client.guest_exec(vm_name, argv, timeout)

    def _wait_ready(self, db: Session, task: Task, cluster: Cluster, progress_from: int, progress_to: int,
                    timeout: float = READY_TIMEOUT, fresh: bool = False) -> Dict[str, Any]:
        """Wait until every node of the cluster is Ready (asked to the first control plane).
        fresh: only count Ready reported since the first control plane booted (after a start)."""
        driver = get_driver(cluster.type)
        first = self._first_ctlplane(cluster)
        expected = {n.name for n in cluster.nodes}
        deadline = time.monotonic() + timeout
        last_error = "k3s is not installed yet"
        since = None
        if fresh:
            boot = self._exec(first.name, ["/bin/sh", "-c", "echo $(( $(date +%s) - $(cut -d. -f1 /proc/uptime) ))"])
            since = datetime.fromtimestamp(int(boot["stdout"].strip()), timezone.utc)
        while True:
            ready: Dict[str, bool] = {}
            try:
                out = self._exec(first.name, driver.ready_nodes_command(), timeout=30)
                if out["exitcode"] == 0:
                    ready = driver.parse_ready(out["stdout"], since)
                else:
                    last_error = (out["stderr"] or out["stdout"]).strip()[-300:] or "API not ready"
            except (libvirt.libvirtError, TimeoutError, KeyError, ValueError) as e:
                last_error = str(e)
            count = sum(1 for name in expected if ready.get(name))
            span = progress_to - progress_from
            self._check(task, cluster, db, progress_from + span * count // max(len(expected), 1),
                        f"Waiting for nodes: {count}/{len(expected)} Ready")
            if count == len(expected):
                return {"nodes_ready": count}
            if time.monotonic() > deadline:
                raise TimeoutError(f"Only {count}/{len(expected)} nodes Ready after {timeout // 60:.0f} min "
                                   f"(last: {last_error})")
            self._sleep(task, 5)

    def _fetch_kubeconfig(self, db: Session, cluster: Cluster) -> None:
        driver = get_driver(cluster.type)
        first = self._first_ctlplane(cluster)
        out = self._exec(first.name, driver.kubeconfig_command(), timeout=30)
        if out["exitcode"] != 0:
            raise RuntimeError(f"Cannot read the kubeconfig on {first.name}: {out['stderr'].strip()}")
        cluster.kubeconfig = self._rewrite_kubeconfig(out["stdout"], cluster)
        try:
            version_out = self._exec(first.name, driver.version_command(), timeout=30)
            version = getattr(driver, "parse_version", lambda _o: None)(version_out["stdout"])
            if version:
                cluster.version = version
        except (libvirt.libvirtError, TimeoutError):
            pass
        db.commit()

    @staticmethod
    def _rewrite_kubeconfig(text: str, cluster: Cluster) -> str:
        """Point the server at the API address reachable from the host and name things after the cluster"""
        config = yaml.safe_load(text)
        name = cluster.name
        for entry in config.get("clusters") or []:
            entry["name"] = name
            entry["cluster"]["server"] = f"https://{cluster.api_ip}:6443"
        for entry in config.get("users") or []:
            entry["name"] = f"{name}-admin"
        for entry in config.get("contexts") or []:
            entry["name"] = name
            entry["context"]["cluster"] = name
            entry["context"]["user"] = f"{name}-admin"
        config["current-context"] = name
        return yaml.safe_dump(config, sort_keys=False)

    # ------------------------------------------------------- day-2 operations

    def _ensure_idle(self, cluster: Cluster) -> None:
        if task_service.is_running(cluster.task_id):
            raise ValueError("The cluster is busy (a task is running): wait for it or cancel it")

    def _run_task(self, db: Session, cluster: Cluster, kind: str, label: str, func: Callable, *args) -> Task:
        self._ensure_idle(cluster)
        task = task_service.start(db, TaskCreate(name=f"{label} cluster {cluster.name}", type=f"cluster_{kind}",
                                                 target_type="cluster", target_id=cluster.id,
                                                 target_name=cluster.name), func, cluster.id, *args)
        cluster.task_id = task.id
        db.commit()
        return task

    def start_cluster(self, db: Session, cluster: Cluster) -> Task:
        def run(db: Session, task: Task, cluster_id: int) -> Dict[str, Any]:
            def body(cluster: Cluster) -> Dict[str, Any]:
                self._set_status(db, cluster, "starting", "Starting nodes")
                network_for(cluster).ensure()
                for role in ("ctlplane", "worker"):
                    for node in cluster.nodes:
                        live = libvirt_client.get_vm(node.name)
                        if node.role == role and live and live["state"] == "shutoff":
                            libvirt_client.start_vm(node.name)
                self._wait_agent(task, self._first_ctlplane(cluster).name, timeout=5 * 60)
                result = self._wait_ready(db, task, cluster, 20, 95, timeout=10 * 60, fresh=True)
                if not cluster.kubeconfig:
                    self._fetch_kubeconfig(db, cluster)
                self._set_status(db, cluster, "ready", None)
                return result
            return self._guarded(db, task, cluster_id, body)
        return self._run_task(db, cluster, "start", "Start", run)

    def stop_cluster(self, db: Session, cluster: Cluster) -> Task:
        def run(db: Session, task: Task, cluster_id: int) -> Dict[str, Any]:
            def body(cluster: Cluster) -> Dict[str, Any]:
                self._set_status(db, cluster, "stopping", "Shutting down nodes")
                # workers first, then control planes
                for role in ("worker", "ctlplane"):
                    names = [n.name for n in cluster.nodes if n.role == role]
                    for name in names:
                        live = libvirt_client.get_vm(name)
                        if live and live["state"] == "running":
                            libvirt_client.stop_vm(name)
                    self._wait_off(task, names)
                self._set_status(db, cluster, "stopped", None)
                return {"stopped": len(cluster.nodes)}
            return self._guarded(db, task, cluster_id, body)
        return self._run_task(db, cluster, "stop", "Stop", run)

    def _wait_off(self, task: Task, names: List[str]) -> None:
        deadline = time.monotonic() + STOP_TIMEOUT
        while True:
            running = [n for n in names if (libvirt_client.get_vm(n) or {}).get("state") not in (None, "shutoff")]
            if not running:
                return
            if time.monotonic() > deadline:
                for name in running:
                    libvirt_client.stop_vm(name, force=True)
                return
            self._sleep(task, 2)

    def add_workers(self, db: Session, cluster: Cluster, count: int) -> Task:
        if not cluster.token:
            raise ValueError("This cluster has no join token (rebuilt from libvirt): can't add workers")
        if cluster.status not in ("ready",):
            raise ValueError("Start the cluster first")

        def run(db: Session, task: Task, cluster_id: int, count: int) -> Dict[str, Any]:
            def body(cluster: Cluster) -> Dict[str, Any]:
                indexes = [int(n.name.rsplit("-", 1)[1]) for n in cluster.nodes
                           if n.role == "worker" and n.name.rsplit("-", 1)[1].isdigit()]
                names = self._node_names(cluster.name, 0, count, max(indexes, default=-1) + 1)
                self._set_status(db, cluster, "provisioning", f"Adding {', '.join(names)}")
                self._add_nodes(db, task, cluster, network_for(cluster), [(n, "worker") for n in names], 5, 30)
                result = self._wait_ready(db, task, cluster, 40, 95)
                cluster.spec = {**cluster.spec, "workers": sum(1 for n in cluster.nodes if n.role == "worker")}
                self._set_status(db, cluster, "ready", None)
                return {**result, "added": names}
            return self._guarded(db, task, cluster_id, body)
        return self._run_task(db, cluster, "scale", "Add workers to", run, count)

    def remove_node(self, db: Session, cluster: Cluster, node_name: str) -> Task:
        node = next((n for n in cluster.nodes if n.name == node_name), None)
        if node is None:
            raise LookupError(f"Node '{node_name}' is not part of cluster {cluster.name}")
        if node.role != "worker":
            raise ValueError("Only workers can be removed")

        def run(db: Session, task: Task, cluster_id: int, node_name: str) -> Dict[str, Any]:
            def body(cluster: Cluster) -> Dict[str, Any]:
                node = next(n for n in cluster.nodes if n.name == node_name)
                previous = cluster.status
                self._set_status(db, cluster, "provisioning", f"Removing {node_name}")
                driver = get_driver(cluster.type)
                first = self._first_ctlplane(cluster)
                drained = False
                try:  # best effort: the cluster may be stopped
                    self._exec(first.name, driver.kubectl_command(
                        ["drain", node_name, "--ignore-daemonsets", "--delete-emptydir-data", "--force",
                         "--timeout=90s"]), timeout=120)
                    out = self._exec(first.name, driver.kubectl_command(["delete", "node", node_name]), timeout=60)
                    drained = out["exitcode"] == 0
                except (libvirt.libvirtError, TimeoutError) as e:
                    logger.warning(f"Could not drain/delete {node_name} in the cluster: {e}")
                self._delete_node_resources(cluster, node)
                cluster.nodes.remove(node)
                cluster.spec = {**cluster.spec, "workers": sum(1 for n in cluster.nodes if n.role == "worker")}
                db.commit()
                vm_service.sync_vms(db)
                self._set_status(db, cluster, previous if previous in ("ready", "stopped") else "ready", None)
                return {"removed": node_name, "deleted_from_kubernetes": drained}
            return self._guarded(db, task, cluster_id, body)
        return self._run_task(db, cluster, "scale", f"Remove {node_name} from", run, node_name)

    def _delete_node_resources(self, cluster: Cluster, node: ClusterNode, whole_cluster: bool = False) -> None:
        network = network_for(cluster)
        if not (whole_cluster and network.owned):  # an owned network goes away with the cluster anyway
            try:
                if node.mac:
                    network.release(node.mac)
                if node.ip:
                    network.unpublish(node.ip)
            except libvirt.libvirtError as e:
                logger.warning(f"Could not remove the records of {node.name} from {cluster.network}: {e}")
        libvirt_client.delete_vm(node.name, delete_disks=True)

    def delete_cluster(self, db: Session, cluster: Cluster) -> None:
        """Delete node VMs + disks, their reservations / records and the cluster's own network"""
        if task_service.is_running(cluster.task_id):
            task_service.cancel_task(db, cluster.task_id)
            if not task_service.wait(cluster.task_id, 60):
                raise ValueError("The cluster task did not stop in time, try again")
            db.refresh(cluster)
        for node in list(cluster.nodes):
            self._delete_node_resources(cluster, node, whole_cluster=True)
        network = network_for(cluster)
        if network.owned:
            network.destroy()
        name, cluster_id = cluster.name, cluster.id
        db.delete(cluster)
        db.commit()
        vm_service.sync_vms(db)
        event_bus.publish({"kind": "cluster", "id": cluster_id, "name": name, "status": "deleted"})

    def get_kubeconfig(self, db: Session, cluster: Cluster) -> str:
        if not cluster.kubeconfig:
            if not cluster.api_ip:
                raise ValueError("The cluster has no API address yet")
            self._fetch_kubeconfig(db, cluster)  # e.g. a cluster rebuilt from libvirt metadata
        return cluster.kubeconfig

    def kubectl(self, db: Session, cluster: Cluster, view: str) -> Dict[str, Any]:
        if view not in KUBECTL_VIEWS:
            raise ValueError(f"Unknown view '{view}' ({', '.join(KUBECTL_VIEWS)})")
        driver = get_driver(cluster.type)
        first = self._first_ctlplane(cluster)
        if first is None:
            raise ValueError("The cluster has no control plane")
        argv = driver.kubectl_command(KUBECTL_VIEWS[view])
        try:
            out = self._exec(first.name, argv, timeout=30)
        except libvirt.libvirtError as e:
            raise ValueError(f"{first.name}: {e.get_error_message() or e} (is the node running?)")
        return {"command": "kubectl " + " ".join(KUBECTL_VIEWS[view]), "node": first.name, **out}


cluster_service = ClusterService()
