"""Kubernetes clusters (k3s, kubeadm) built from cloud-image VMs

A cluster = node VMs created with vm_service (cloud image + cloud-init) on a
ClusterNetwork (fixed MAC -> IP reservations, DNS records for the nodes and
api.<cluster>.<domain>), plus a clusters row for what only the app knows (join
token, kubeconfig, spec). k3s clusters get a standalone libvirt network (no
router); kubeadm clusters live in a lab group whose router serves their DNS and
fronts the API with haproxy (GroupClusterNetwork). Each node VM carries
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
from sqlalchemy.orm.exc import ObjectDeletedError

from app.database import serialized
from app.events import event_bus
from app.libvirt_client import libvirt_client
from app.models import VM, CloudImage, Cluster, ClusterNode, Group, Task
from app.schemas import ClusterCreate, TaskCreate, VMCreate
from app.services.cloud_image_service import cloud_image_service
from app.services.cluster_drivers import EL_DISTRIBUTIONS, ClusterDriver, get_driver, kubeadm_token
from app.services.cluster_network import ClusterNetwork, GroupClusterNetwork, LibvirtClusterNetwork, free_subnet
from app.services.group_service import group_service, network_name as group_network_name
from app.services.task_service import task_service
from app.services.vm_service import vm_service

logger = logging.getLogger(__name__)

METADATA_URI = "urn:vm-manager:cluster"
METADATA_KEY = "vmmc"

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


GROUP_CREATE_TIMEOUT = 20 * 60  # auto-created group: router first boot (dnf)
API_PORT = 6443
# Never returned by the API
SECRET_SPEC_KEYS = ("password", "certificate_key", "iso_path")


def network_for(cluster: Cluster) -> ClusterNetwork:
    """The network implementation of a cluster: its lab group (kubeadm) or a libvirt network (k3s)"""
    if cluster.group_id:
        return GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))
    spec = cluster.spec or {}
    return LibvirtClusterNetwork(cluster.network, owned=bool(cluster.network_owned),
                                 cidr=spec.get("cidr"), zone=f"{cluster.name}.{cluster.domain}")


def _node_index(name: str) -> int:
    suffix = name.rsplit("-", 1)[-1]
    return int(suffix) if suffix.isdigit() else -1


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
        if any(e["attrs"].get("group") for e in found.values()):
            group_service.sync_groups(db)
        for name, entry in found.items():
            cluster = db.query(Cluster).filter(Cluster.name == name).first()
            if cluster is None:
                group = None
                if entry["attrs"].get("group"):
                    group = db.query(Group).filter(Group.name == entry["attrs"]["group"]).first()
                ctype = entry["attrs"].get("type", "k3s")
                spec = self._rebuild_spec(entry["nodes"])
                message = ("Rebuilt from libvirt metadata (kubeconfig and join token are read "
                           "from the first control plane when needed)")
                if ctype == "openshift":
                    from app.services.openshift_installer import openshift_installer
                    saved = openshift_installer.load_spec(name)
                    spec = saved or spec
                    message = None if saved else "Rebuilt from libvirt metadata (add-on settings unknown)"
                cluster = Cluster(
                    group_id=group.id if group else None,
                    group_owned=entry["attrs"].get("group_owned") == "yes" if group else None,
                    name=name, type=ctype, status="ready", adopted=True,
                    version=entry["attrs"].get("version") or None,
                    network=entry["attrs"].get("network", "default"),
                    network_owned=entry["attrs"].get("owned") == "yes",
                    domain=entry["attrs"].get("domain", "lab"),
                    status_message=message,
                    spec=spec,
                )
                db.add(cluster)
                db.flush()
                changed = True
            known = {n.name for n in cluster.nodes}
            # ctlplane-0 first: it is the API endpoint and where kubectl runs
            entry["nodes"].sort(key=lambda n: (n[1].get("role") != "ctlplane", _node_index(n[0])))
            for vm_name, attrs in entry["nodes"]:
                if vm_name in known:
                    continue
                nics = libvirt_client.get_vm_nics(vm_name)
                cluster.nodes.append(ClusterNode(name=vm_name, role=attrs.get("role", "worker"),
                                                 ip=attrs.get("ip"), mac=nics[0]["mac"] if nics else None))
                changed = True
            if not cluster.api_ip:
                first = self._first_ctlplane(cluster)
                if cluster.group_id:
                    try:
                        cluster.api_ip = network_for(cluster).uplink_ip()
                    except ValueError:
                        pass
                elif first is not None:
                    cluster.api_ip = first.ip
                changed = changed or bool(cluster.api_ip)
        # adopted clusters (rebuilt from metadata) whose node VMs are all gone: forget them
        for cluster in db.query(Cluster).filter(Cluster.adopted.is_(True)).all():
            if cluster.name not in found and not task_service.is_running(cluster.task_id):
                logger.info(f"Forgot cluster {cluster.name}: adopted from libvirt metadata, no node left")
                event_bus.publish({"kind": "cluster", "id": cluster.id, "name": cluster.name, "status": "deleted"})
                db.delete(cluster)
                changed = True
        if changed:
            db.commit()

    @staticmethod
    def _rebuild_spec(nodes: List[tuple]) -> Dict[str, Any]:
        """Best-effort creation spec from node metadata + VM sizes (no password / SSH keys)"""
        attrs = nodes[0][1]
        spec: Dict[str, Any] = {
            "cidr": attrs.get("cidr") or None, "pod_cidr": attrs.get("pod_cidr") or "10.244.0.0/16",
            "service_cidr": attrs.get("service_cidr") or "10.96.0.0/16", "el": attrs.get("el") == "yes",
            "cloud_image_id": int(attrs["image"]) if attrs.get("image", "").isdigit() else None,
            "ctlplanes": sum(1 for _, a in nodes if a.get("role") == "ctlplane"),
            "workers": sum(1 for _, a in nodes if a.get("role") == "worker"),
            "username": None, "ssh_keys": [], "rebuilt": True,
            "api_port": int(attrs["api_port"]) if attrs.get("api_port", "").isdigit() else API_PORT,
        }
        for role in ("ctlplane", "worker"):
            res = {"memory": 2048, "vcpu": 2, "disk_size": 20}
            vm_name = next((n for n, a in nodes if a.get("role") == role), None)
            live = libvirt_client.get_vm(vm_name) if vm_name else None
            if live:
                res.update(memory=live["max_memory"] // 1024, vcpu=live["vcpu"])
            spec[role] = res
        return spec

    def list_clusters(self, db: Session) -> List[Dict[str, Any]]:
        self.sync_clusters(db)
        vm_service.sync_vms(db)
        states = {vm["name"]: vm["state"] for vm in libvirt_client.list_vms()}
        result = []
        for c in db.query(Cluster).order_by(Cluster.name).all():
            try:
                result.append(self.to_dict(db, c, states))
            except ObjectDeletedError:
                continue  # deleted by another request meanwhile
        return result

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

        spec = {k: v for k, v in (cluster.spec or {}).items() if k not in SECRET_SPEC_KEYS}
        port = self._api_port(cluster)
        group = db.query(Group).filter(Group.id == cluster.group_id).first() if cluster.group_id else None
        lb = None
        if group is not None:
            gspec = group.spec or {}
            entry = next((x for x in gspec.get("load_balancers") or []
                          if x.get("owner") == f"cluster:{cluster.name}"), None)
            if entry:
                lb = {"name": entry["name"], "port": entry["port"], "backends": entry["backends"],
                      "router_ip": (gspec.get("router") or {}).get("ip"),
                      "uplink_ip": (gspec.get("router") or {}).get("uplink_ip")}
        return {
            "id": cluster.id, "name": cluster.name, "type": cluster.type, "version": cluster.version,
            "network": cluster.network, "network_owned": bool(cluster.network_owned), "domain": cluster.domain,
            "group_id": cluster.group_id, "group_name": group.name if group else None,
            "group_owned": bool(cluster.group_owned), "load_balancer": lb,
            "api_hostname": f"api.{zone}", "api_ip": cluster.api_ip,
            "api_endpoint": f"https://{cluster.api_ip}:{port}" if cluster.api_ip else None,
            "status": status, "status_message": cluster.status_message, "task_id": cluster.task_id,
            "task_running": busy, "task_progress": task_progress,
            "has_kubeconfig": bool(cluster.kubeconfig),
            # OpenShift: only once installed (the name doesn't answer before)
            "console_url": (f"https://console-openshift-console.apps.{zone}"
                            if cluster.type == "openshift" and status in ("ready", "stopped", "starting", "stopping")
                            else None),
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

    @staticmethod
    def _api_node(cluster: Cluster) -> Optional[ClusterNode]:
        """Control plane to run kubectl / kubeadm on: the first running one (with 3 control
        planes the cluster keeps working while ctlplane-0 is down)"""
        ctlplanes = [n for n in cluster.nodes if n.role == "ctlplane"]
        for node in ctlplanes:
            if (libvirt_client.get_vm(node.name) or {}).get("state") == "running":
                return node
        return ctlplanes[0] if ctlplanes else None

    @staticmethod
    def _api_port(cluster: Cluster) -> int:
        return int((cluster.spec or {}).get("api_port") or API_PORT)

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
        if data.type == "openshift":
            if data.network:
                raise ValueError("OpenShift clusters live in a lab group: pick a group_id or let the app create one")
            if db.query(Cluster).filter(Cluster.name == data.name).first():
                raise ValueError(f"Cluster '{data.name}' already exists")
            from app.services.openshift_installer import openshift_installer
            return openshift_installer.create(db, data)
        if data.openshift is not None:
            raise ValueError("'openshift' options are for type openshift")
        driver = get_driver(data.type)  # rejects unsupported types
        if driver.needs_group and data.network:
            raise ValueError(f"{data.type} clusters live in a lab group (router DNS + API load balancer): "
                             "pick a group_id or let the app create one, not a network")
        if not driver.needs_group and data.group_id is not None:
            raise ValueError(f"{data.type} clusters use their own standalone network (no router): "
                             "group_id is for kubeadm clusters")
        if data.type == "kubeadm":
            from app.services.cluster_drivers import kubeadm_version
            kubeadm_version(data.version)  # validates
        elif data.version and (not data.version.startswith("v") or data.version.count(".") != 2):
            raise ValueError("k3s version: a release such as v1.33.5+k3s1")
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

        if driver.needs_group:
            return self._create_in_group(db, data, image)

        zone = f"{data.name}.{data.domain}"
        if data.network:
            network = LibvirtClusterNetwork.existing(data.network)
        else:
            owned_name = LibvirtClusterNetwork.owned_name(data.name)
            if any(n["name"] == owned_name for n in libvirt_client.list_networks()):
                raise ValueError(f"Network '{owned_name}' already exists")
            network = LibvirtClusterNetwork.for_new_cluster(data.name, zone, data.cidr,
                                                            avoid=[data.pod_cidr, data.service_cidr]
                                                            + [g.cidr for g in db.query(Group).all()])
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

    def _create_in_group(self, db: Session, data: ClusterCreate, image: CloudImage) -> Cluster:
        """kubeadm: nodes in an existing lab group, or in a group created for (and deleted with) the cluster"""
        names = self._node_names(data.name, data.ctlplanes, data.workers)
        group, gspec, owned, node_subnet = self._resolve_group(db, data, names, [data.pod_cidr, data.service_cidr])
        task = None
        if group is None:
            group, task = group_service.create_group(db, gspec)

        spec = data.model_dump()
        spec.update(cloud_image_id=image.id, image=f"{image.distribution} {image.version}",
                    el=image.distribution in EL_DISTRIBUTIONS, cidr=str(node_subnet),
                    certificate_key=secrets.token_hex(32), group_task_id=task.id if task else None)
        spec.pop("network", None)
        cluster = Cluster(
            name=data.name, type=data.type, version=data.version, network=group_network_name(group.name),
            network_owned=False, group_id=group.id, group_owned=owned, domain=gspec.domain, spec=spec,
            token=kubeadm_token(), status="provisioning",
            status_message="Creating the lab group (router first boot)" if owned else "Queued",
        )
        db.add(cluster)
        db.commit()
        db.refresh(cluster)
        self._publish(cluster)
        where = f"new lab group {group.name}" if owned else f"lab group {group.name}"
        task = task_service.start(db, TaskCreate(
            name=f"Create {data.type} cluster {data.name}", type="cluster_create", target_type="cluster",
            target_id=cluster.id, target_name=data.name,
            description=f"{data.ctlplanes} control plane(s) + {data.workers} worker(s), {spec['image']}, {where}",
        ), self._provision, cluster.id)
        cluster.task_id = task.id
        db.commit()
        return cluster

    def _resolve_group(self, db: Session, data: ClusterCreate, node_names: List[str],
                       cluster_cidrs: List[str], check: Optional[Callable[[Any], None]] = None) -> tuple:
        """Validate the lab group of a new cluster: an existing one (data.group_id) or the spec of one
        to create, named after the cluster. cluster_cidrs: in-cluster networks the node network must
        not overlap. check(gspec): extra validation of an existing group's spec.
        Returns (group or None, GroupSpec, owned, node subnet); nothing is created."""
        from app.schemas.group import CloudInitSpec, GroupSpec, RouterSpec
        group_service.sync_groups(db)
        nets = [ipaddress.IPv4Network(c, strict=False) for c in cluster_cidrs]
        if data.group_id is not None:
            group = group_service.get_group(db, data.group_id)
            if group is None:
                raise ValueError(f"Lab group {data.group_id} not found")
            if group.status in ("creating", "deleting", "missing", "error"):
                raise ValueError(f"Lab group {group.name} is {group.status}")
            gspec = GroupSpec.model_validate(group.spec)
            if not gspec.uplink:
                raise ValueError(f"Lab group {group.name} has no uplink: the host could not reach the API")
            names = {m.name for m in gspec.members} | {r.name for r in gspec.reservations}
            clash = [n for n in node_names if n in names]
            if clash:
                raise ValueError(f"'{clash[0]}' already exists in group {group.name}")
            if check is not None:
                check(gspec)
            node_subnet = ipaddress.IPv4Network(gspec.cidr)
            owned = False
        else:
            if len(data.name) > 32:
                raise ValueError("The cluster name is also its lab group's name: 32 characters max")
            if db.query(Group).filter(Group.name == data.name).first():
                raise ValueError(f"A lab group named '{data.name}' already exists: pick it (group_id) "
                                 "or another cluster name")
            avoid = list(cluster_cidrs) + [g.cidr for g in db.query(Group).all()]
            if data.cidr:
                node_subnet = ipaddress.IPv4Network(data.cidr, strict=False)
                if any(node_subnet.overlaps(ipaddress.IPv4Network(c, strict=False)) for c in avoid):
                    raise ValueError(f"{node_subnet} overlaps an in-cluster network or another group")
            else:
                node_subnet = free_subnet(avoid)
            router = RouterSpec(memory=data.router_memory) if data.router_memory else RouterSpec()
            gspec = GroupSpec(
                name=data.name, cidr=str(node_subnet), domain=data.domain, owner=f"cluster:{data.name}",
                router=router,
                cloud_init=CloudInitSpec(username=data.username, password=data.password,
                                         ssh_keys=data.ssh_keys, keyboard=data.keyboard),
            )
            group, owned = None, True
        for net in nets:
            if net.overlaps(node_subnet):
                raise ValueError(f"The in-cluster network {net} overlaps the node network {node_subnet}")
        existing_vms = {vm["name"] for vm in libvirt_client.list_vms()}
        clash = [n for n in node_names if n in existing_vms]
        if clash:
            raise ValueError(f"VM '{clash[0]}' already exists")
        return group, gspec, owned, node_subnet

    def _wait_group(self, db: Session, task: Task, cluster: Cluster) -> None:
        """Auto-created group: wait for its creation task (router first boot)"""
        deadline = time.monotonic() + GROUP_CREATE_TIMEOUT
        started = time.monotonic()
        while True:
            db.expire_all()
            group = db.query(Group).filter(Group.id == cluster.group_id).first()
            if group is None:
                raise ValueError("The cluster's lab group was deleted")
            if group.status == "ready":
                return
            if group.status in ("error", "missing", "deleting"):
                raise RuntimeError(f"Lab group {group.name}: {group.status}"
                                   + (f" ({group.error_message})" if group.error_message else ""))
            if time.monotonic() > deadline:
                raise TimeoutError(f"Lab group {group.name} not ready after {GROUP_CREATE_TIMEOUT // 60} min")
            elapsed = time.monotonic() - started
            self._check(task, cluster, db, 2 + int(18 * min(elapsed / 240, 1)),
                        f"Creating lab group {group.name} (router first boot)")
            self._sleep(task, 3)

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
            driver = get_driver(cluster.type)
            if cluster.group_owned:
                self._wait_group(db, task, cluster)
            self._check(task, cluster, db, 21 if cluster.group_id else 2,
                        "Starting the lab group's router" if cluster.group_id else "Creating network")
            network = network_for(cluster)
            network.ensure()

            names = self._node_names(cluster.name, spec["ctlplanes"], spec["workers"])
            roles = ["ctlplane"] * spec["ctlplanes"] + ["worker"] * spec["workers"]
            p_from, p_to = (22, 30) if cluster.group_id else (5, 30)
            nodes = self._add_nodes(db, task, cluster, network, list(zip(names, roles)), p_from, p_to)

            first = self._first_ctlplane(cluster)
            self._check(task, cluster, db, 32, f"Waiting for the guest agent of {first.name} (first boot)")
            self._wait_agent(task, first.name)
            if driver.orchestrated:
                driver.bootstrap(self._ops(db, task, cluster), self._ctx(cluster),
                                 self._node_dicts(cluster, nodes), initial=True)
                result = self._wait_ready(db, task, cluster, 80, 92)
            else:
                result = self._wait_ready(db, task, cluster, 40, 92)

            self._check(task, cluster, db, 95, "Fetching kubeconfig")
            self._fetch_kubeconfig(db, cluster)
            self._set_status(db, cluster, "ready", None)
            return {**result, "api_endpoint": f"https://{cluster.api_ip}:{self._api_port(cluster)}",
                    "version": cluster.version}

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
        new_ctlplanes = [n for n in created if n.role == "ctlplane"]
        if network.fronts_api and new_ctlplanes:
            # The router's haproxy fronts every control plane; the host reaches it on the router's
            # uplink address, the nodes through api(-int).<cluster>.<domain> -> router LAN address
            port = spec.get("api_port") or network.free_lb_port(API_PORT)
            cluster.spec = {**spec, "api_port": port}
            spec = cluster.spec
            ips = [n.ip for n in cluster.nodes if n.role == "ctlplane"]
            cluster.api_ip = network.publish_api([api_hostname, f"api-int.{zone}"], ips, port)
        elif not network.fronts_api:
            cluster.api_ip = first.ip
        db.commit()

        ctx = self._ctx(cluster)
        step = (progress_to - progress_from) / max(len(created), 1)
        for node in created:
            network.reserve(node.name, node.mac, node.ip)
            hostnames = [f"{node.name}.{zone}", node.name]
            if node is first and not network.fronts_api:
                hostnames += [api_hostname, f"api-int.{zone}"]
            network.publish(node.ip, hostnames)
        network.commit()  # group: one router config push for all the nodes
        for i, node in enumerate(created):
            self._check(task, cluster, db, int(progress_from + i * step), f"Creating {node.name}")
            self._create_node_vm(db, cluster, driver, ctx, node, node is first)
        for node in sorted(created, key=lambda n: n.role != "ctlplane"):
            libvirt_client.start_vm(node.name)
        return created

    def _ctx(self, cluster: Cluster) -> Dict[str, Any]:
        """What drivers need to render a node's cloud-init / bootstrap the cluster"""
        spec = cluster.spec
        zone = f"{cluster.name}.{cluster.domain}"
        api_hostname = f"api.{zone}"
        ctx = {
            "name": cluster.name, "zone": zone, "api_hostname": api_hostname, "api_ip": cluster.api_ip,
            "api_port": self._api_port(cluster),
            "token": cluster.token, "version": cluster.version or spec.get("version"),
            "ctlplanes": spec["ctlplanes"], "pod_cidr": spec["pod_cidr"], "service_cidr": spec["service_cidr"],
            "extra_args": spec.get("extra_args"), "el": spec.get("el"),
            "certificate_key": spec.get("certificate_key"),
        }
        if cluster.group_id:
            router_ip = network_for(cluster).gateway()
            ctx["api_sans"] = [n for n in (api_hostname, f"api-int.{zone}", cluster.api_ip, router_ip) if n]
        return ctx

    @staticmethod
    def _node_dicts(cluster: Cluster, nodes: List[ClusterNode]) -> List[Dict[str, Any]]:
        first = next((n for n in cluster.nodes if n.role == "ctlplane"), None)
        ordered = sorted(nodes, key=lambda n: (n.role != "ctlplane", _node_index(n.name)))
        return [{"name": n.name, "role": n.role, "ip": n.ip, "first": n is first} for n in ordered]

    def _ops(self, db: Session, task: Task, cluster: Cluster) -> Any:
        """What a driver's bootstrap() may do: run commands in nodes, wait, report progress"""
        service = self

        class Ops:
            @staticmethod
            def exec(vm: str, argv: List[str], timeout: float = 60) -> Dict[str, Any]:
                if task_service.is_cancelled(task.id):
                    raise Cancelled()
                return service._exec(vm, argv, timeout=timeout)

            @staticmethod
            def wait_agent(vm: str) -> None:
                service._wait_agent(task, vm)

            @staticmethod
            def progress(pct: int, message: str) -> None:
                service._check(task, cluster, db, pct, message)

            @staticmethod
            def sleep(seconds: float) -> None:
                service._sleep(task, seconds)

            @staticmethod
            def api_node() -> str:
                return service._api_node(cluster).name

        return Ops()

    def _create_node_vm(self, db: Session, cluster: Cluster, driver: ClusterDriver, ctx: Dict[str, Any],
                        node: ClusterNode, first: bool) -> None:
        spec = cluster.spec
        res = spec["ctlplane"] if node.role == "ctlplane" else spec["worker"]
        fqdn = f"{node.name}.{cluster.name}.{cluster.domain}"
        config = cloud_image_service.build_cloud_config(
            hostname=node.name,
            username=spec.get("username"),
            password=spec.get("password"),
            ssh_keys=[k.strip() for k in spec.get("ssh_keys") or [] if k.strip()],
            # console-setup (used for the layout) only exists on Debian/Ubuntu
            keyboard=None if spec.get("el") else spec.get("keyboard"),
            fqdn=fqdn,
        )
        # The driver's additions: lists (packages, runcmd, write_files) are appended
        extra = driver.node_cloud_config(ctx, {"name": node.name, "role": node.role, "ip": node.ip, "first": first})
        for key, value in extra.items():
            config[key] = config[key] + value if isinstance(value, list) and isinstance(config.get(key), list) else value
        attrs = {"name": cluster.name, "type": cluster.type, "role": node.role, "ip": node.ip,
                 "domain": cluster.domain, "network": cluster.network,
                 "owned": "yes" if cluster.network_owned else "no", "cidr": spec.get("cidr") or "",
                 "image": spec.get("cloud_image_id") or "", "el": "yes" if spec.get("el") else "no",
                 "pod_cidr": spec.get("pod_cidr") or "", "service_cidr": spec.get("service_cidr") or "",
                 "version": cluster.version or ""}
        if cluster.group_id:
            group = db.query(Group).filter(Group.id == cluster.group_id).first()
            attrs.update(group=group.name if group else "", group_owned="yes" if cluster.group_owned else "no",
                         api_port=self._api_port(cluster))
        metadata = (f"<{METADATA_KEY}:cluster xmlns:{METADATA_KEY}={quoteattr(METADATA_URI)} "
                    + " ".join(f"{k}={quoteattr(str(v))}" for k, v in attrs.items()) + "/>")
        vm_service.create_vm(
            db, VMCreate(
                name=node.name, description=f"{cluster.type} cluster {cluster.name}: {node.role}",
                memory=res["memory"], vcpu=res["vcpu"], disk_size=res["disk_size"],
                cloud_image_id=spec["cloud_image_id"], network_name=cluster.network, mac_address=node.mac,
                os_type=spec.get("image"), start=False,
            ),
            fqdn=fqdn, user_data="#cloud-config\n" + yaml.safe_dump(config, sort_keys=False), metadata_xml=metadata,
        )

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
        while not libvirt_client.agent_ping(vm_name):
            if time.monotonic() > deadline:
                raise TimeoutError(f"The QEMU guest agent of {vm_name} did not answer within "
                                   f"{timeout // 60:.0f} min (is qemu-guest-agent installed? check the console)")
            self._sleep(task, 5)

    def _exec(self, vm_name: str, argv: List[str], timeout: float = 60) -> Dict[str, Any]:
        return libvirt_client.agent_exec(vm_name, argv[0], argv[1:], timeout=timeout)

    def _wait_ready(self, db: Session, task: Task, cluster: Cluster, progress_from: int, progress_to: int,
                    timeout: float = READY_TIMEOUT, fresh: bool = False) -> Dict[str, Any]:
        """Wait until every node of the cluster is Ready (asked to the first control plane).
        fresh: only count Ready reported since the first control plane booted (after a start)."""
        driver = get_driver(cluster.type)
        first = self._first_ctlplane(cluster) if fresh else self._api_node(cluster)
        expected = {n.name for n in cluster.nodes}
        deadline = time.monotonic() + timeout
        last_error = f"{cluster.type} is not installed yet"
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
        first = self._api_node(cluster)
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
    def _rewrite_kubeconfig(text: str, cluster: "Cluster") -> str:
        """Point the server at the API address reachable from the host and name things after the cluster"""
        config = yaml.safe_load(text)
        name = cluster.name
        for entry in config.get("clusters") or []:
            entry["name"] = name
            # k3s: the first control plane; kubeadm: the group router's load balancer (uplink address)
            entry["cluster"]["server"] = f"https://{cluster.api_ip}:{ClusterService._api_port(cluster)}"
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
                if cluster.type == "openshift":
                    from app.services.openshift_installer import openshift_installer
                    result = openshift_installer.start(db, task, cluster)
                    self._set_status(db, cluster, "ready", None)
                    return result
                self._set_status(db, cluster, "starting",
                                 "Starting the lab group's router" if cluster.group_id else "Starting nodes")
                network_for(cluster).ensure()
                self._check(task, cluster, db, 10, "Starting nodes")
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
                network_for(cluster).stop()  # auto-created group: its router too
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

    def _recover_token(self, db: Session, cluster: Cluster) -> None:
        """Clusters rebuilt from libvirt metadata have no token: read it from the first control plane"""
        driver = get_driver(cluster.type)
        first = self._api_node(cluster)
        try:
            out = self._exec(first.name, driver.token_command(), timeout=30)
        except (libvirt.libvirtError, TimeoutError) as e:
            raise ValueError(f"No join token and cannot read it from {first.name}: {e}")
        if out["exitcode"] != 0 or not out["stdout"].strip():
            raise ValueError(f"No join token and cannot read it from {first.name}: {out['stderr'].strip()}")
        cluster.token = out["stdout"].strip()
        if cluster.status_message and cluster.status_message.startswith("Rebuilt"):
            cluster.status_message = None
        db.commit()

    def add_workers(self, db: Session, cluster: Cluster, count: int) -> Task:
        if cluster.type == "openshift":
            raise ValueError("Adding workers to an OpenShift cluster is not supported yet")
        if self.to_dict(db, cluster)["status"] != "ready":
            raise ValueError("Start the cluster first")
        if not cluster.token:
            self._recover_token(db, cluster)
        if not (cluster.spec or {}).get("cloud_image_id"):  # rebuilt from old metadata
            image = self._pick_image(db, None)
            cluster.spec = {**(cluster.spec or {}), "cloud_image_id": image.id,
                            "image": f"{image.distribution} {image.version}",
                            "el": image.distribution in EL_DISTRIBUTIONS}
            db.commit()

        def run(db: Session, task: Task, cluster_id: int, count: int) -> Dict[str, Any]:
            def body(cluster: Cluster) -> Dict[str, Any]:
                last = max((_node_index(n.name) for n in cluster.nodes if n.role == "worker"), default=-1)
                names = self._node_names(cluster.name, 0, count, last + 1)
                self._set_status(db, cluster, "provisioning", f"Adding {', '.join(names)}")
                network = network_for(cluster)
                network.ensure()
                added = self._add_nodes(db, task, cluster, network, [(n, "worker") for n in names], 5, 30)
                driver = get_driver(cluster.type)
                if driver.orchestrated:
                    driver.bootstrap(self._ops(db, task, cluster), self._ctx(cluster),
                                     self._node_dicts(cluster, added), initial=False)
                result = self._wait_ready(db, task, cluster, 80 if driver.orchestrated else 40, 95)
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
        if cluster.type == "openshift":
            raise ValueError("Removing OpenShift nodes is not supported yet")

        def run(db: Session, task: Task, cluster_id: int, node_name: str) -> Dict[str, Any]:
            def body(cluster: Cluster) -> Dict[str, Any]:
                node = next(n for n in cluster.nodes if n.name == node_name)
                previous = cluster.status
                self._set_status(db, cluster, "provisioning", f"Removing {node_name}")
                driver = get_driver(cluster.type)
                first = self._api_node(cluster)
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

    def _delete_node_resources(self, cluster: Cluster, node: ClusterNode, whole_cluster: bool = False,
                               network: Optional[ClusterNetwork] = None) -> None:
        network = network or network_for(cluster)
        if not whole_cluster:  # the whole cluster: destroy() drops everything at once
            try:
                if node.mac:
                    network.release(node.mac)
                if node.ip:
                    network.unpublish(node.ip)
                network.commit()
            except (libvirt.libvirtError, ValueError, RuntimeError) as e:
                logger.warning(f"Could not remove the records of {node.name} from {cluster.network}: {e}")
        libvirt_client.delete_vm(node.name, delete_disks=True)

    def delete_cluster(self, db: Session, cluster: Cluster) -> None:
        """Delete node VMs + disks, their reservations / records and the cluster's own network"""
        if task_service.is_running(cluster.task_id):
            task_service.cancel_task(db, cluster.task_id)
            if not task_service.wait(cluster.task_id, 60):
                raise ValueError("The cluster task did not stop in time, try again")
            db.refresh(cluster)
        network = network_for(cluster)
        for node in list(cluster.nodes):
            if not network.owned and not network.fronts_api:
                # existing libvirt network: remove the node's reservation / records one by one
                self._delete_node_resources(cluster, node, network=network)
            else:
                libvirt_client.delete_vm(node.name, delete_disks=True)
        if cluster.group_id:
            if cluster.group_owned:
                if group_service.get_group(db, cluster.group_id) is not None:
                    group_service.delete_group(db, cluster.group_id, for_cluster=cluster.name)
            else:
                try:
                    network.destroy()  # only the cluster's reservations / records / load balancer
                except (ValueError, RuntimeError) as e:
                    logger.warning(f"Could not remove cluster {cluster.name}'s entries from its group: {e}")
        elif network.owned:
            network.destroy()
        if cluster.type == "openshift":
            from app.services.openshift_installer import openshift_installer
            openshift_installer.delete_files(cluster)
        name, cluster_id = cluster.name, cluster.id
        db.delete(cluster)
        db.commit()
        vm_service.sync_vms(db)
        event_bus.publish({"kind": "cluster", "id": cluster_id, "name": name, "status": "deleted"})

    def get_kubeconfig(self, db: Session, cluster: Cluster) -> str:
        if cluster.type == "openshift":
            if not cluster.kubeconfig:
                # rebuilt from libvirt metadata: the install dir still has it
                from app.services.openshift_service import openshift_service
                path = openshift_service.root / "clusters" / cluster.name / "host-kubeconfig"
                if not path.exists():
                    raise ValueError("The cluster has no kubeconfig yet (it is written when the install starts)")
                cluster.kubeconfig = path.read_text()
                db.commit()
            return cluster.kubeconfig
        if not cluster.kubeconfig:
            if not cluster.api_ip:
                raise ValueError("The cluster has no API address yet")
            self._fetch_kubeconfig(db, cluster)  # e.g. a cluster rebuilt from libvirt metadata
        return cluster.kubeconfig

    def kubectl(self, db: Session, cluster: Cluster, view: str) -> Dict[str, Any]:
        if view not in KUBECTL_VIEWS:
            raise ValueError(f"Unknown view '{view}' ({', '.join(KUBECTL_VIEWS)})")
        if cluster.type == "openshift":
            from app.services.openshift_service import openshift_service
            out = openshift_service.oc(cluster.name, cluster.version, KUBECTL_VIEWS[view], 30)
            return {"command": "oc " + " ".join(KUBECTL_VIEWS[view]), "node": "(host, via the router)", **out}
        driver = get_driver(cluster.type)
        first = self._api_node(cluster)
        if first is None:
            raise ValueError("The cluster has no control plane")
        argv = driver.kubectl_command(KUBECTL_VIEWS[view])
        try:
            out = self._exec(first.name, argv, timeout=30)
        except libvirt.libvirtError as e:
            raise ValueError(f"{first.name}: {e.get_error_message() or e} (is the node running?)")
        return {"command": "kubectl " + " ".join(KUBECTL_VIEWS[view]), "node": first.name, **out}


cluster_service = ClusterService()
