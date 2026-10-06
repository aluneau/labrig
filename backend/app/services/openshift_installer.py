"""OpenShift clusters with the agent-based installer (ABI), in a lab group

Layout (platform none, the group router is the external DNS + load balancer, like a customer's
UPI-style datacenter):
  DNS   api / api-int.<cluster>.<domain> -> router LAN IP, *.apps.<cluster>.<domain> -> router LAN IP,
        <node>.<domain> + <node>.<cluster>.<domain> -> node (reservations)
  LB    6443 + 22623 -> masters, 80 + 443 -> ingress nodes (masters when there are no workers)
Ports are fixed, so a group hosts one OpenShift cluster.

Install task: binaries (cached per version) -> reservations / DNS / LBs (one router push) ->
install-config.yaml + agent-config.yaml -> `openshift-install agent create image` -> ISO uploaded to
the default pool -> node VMs (empty disk first, ISO second in the boot order) -> wait: the Assisted
Service on the rendezvous host (master-0, port 8090, polled with curl from the router since the
host has no route to the group network), then the API through the router's haproxy (clusterversion /
clusteroperators, CSR approval) -> eject + delete the ISO -> add-ons.
"""
import ipaddress
import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from xml.sax.saxutils import quoteattr

import libvirt
import yaml
from sqlalchemy.orm import Session

from app import domain_xml
from app.config import settings
from app.libvirt_client import libvirt_client
from app.models import Cluster, ClusterNode, Group, Task
from app.schemas import ClusterCreate, TaskCreate, VMCreate
from app.schemas.openshift import OpenShiftOptions
from app.schemas.registry import MirrorRequest, OperatorCatalog, OperatorPackage
from app.schemas.vm import VMNicCreate
from app.services.cluster_network import GroupClusterNetwork
from app.services.group_service import group_service, network_name as group_network_name, router_vm_name
from app.schemas.group import BGP_PEER_ASN
from app.services.openshift_addons import DEMO_IMAGE, STORAGE_SERIAL, AddonRunner, metallb_pool
from app.services import registry_service
from app.services.openshift_service import CATALOG_INDEXES, POD_CIDR, RESERVED_CIDRS, SERVICE_CIDR, openshift_service
from app.services.task_service import task_service
from app.services.vm_service import vm_service

logger = logging.getLogger(__name__)

LB_PORTS = {"api": 6443, "mcs": 22623, "http": 80, "https": 443}
ASSISTED_PORT = 8090
INSTALL_TIMEOUT = 150 * 60     # boot + install + cluster operators (a nested rig can be slow)
IMAGE_TIMEOUT = 30 * 60        # agent create image (first run downloads the 1.4 GB base ISO)
START_TIMEOUT = 20 * 60
CATALOG_TIMEOUT = 15 * 60      # disconnected: mirrored CatalogSource READY

# Defaults per role, used when the request doesn't size the nodes (MiB / vCPU / GiB)
DEFAULTS = {
    "sno": {"memory": 24576, "vcpu": 8, "disk_size": 120},
    "master": {"memory": 20480, "vcpu": 8, "disk_size": 120},
    "worker": {"memory": 12288, "vcpu": 4, "disk_size": 120},
}
# Assisted Service host validations (lower = the install refuses to start)
MINIMUMS = {
    "sno": {"memory": 16384, "vcpu": 8, "disk_size": 100},
    "master": {"memory": 16384, "vcpu": 4, "disk_size": 100},
    "worker": {"memory": 8192, "vcpu": 2, "disk_size": 100},
}
# Per storage node. lean = Red Hat's resourceProfile; lab = small Ceph limits, no NooBaa / RGW (openshift_addons.odf)
ODF_EXTRA = {"lab": {"memory": 6144, "vcpu": 2}, "lean": {"memory": 24576, "vcpu": 8}}


class _BlockDumper(yaml.SafeDumper):
    """install-config.yaml: multi-line strings (additionalTrustBundle) as | blocks"""


_BlockDumper.add_representer(str, lambda d, v: d.represent_scalar(
    "tag:yaml.org,2002:str", v, style="|" if "\n" in v else None))


def sriov_network_name(cluster_name: str) -> str:
    """Isolated L2 network of the cluster's igb (SR-IOV) NICs: no IP, no DHCP, so the extra NICs don't
    get a second address in the machine network (the installer rejects overlapping networks)"""
    return f"vmm-s-{cluster_name}"


def topology_counts(opts: OpenShiftOptions, workers: int) -> Dict[str, int]:
    if opts.topology == "sno":
        return {"masters": 1, "workers": 0}
    if opts.topology == "compact":
        return {"masters": 3, "workers": 0}
    if workers < 2:
        raise ValueError("HA topology: at least 2 workers (or pick compact: 3 schedulable masters)")
    return {"masters": 3, "workers": workers}


def storage_roles(opts: OpenShiftOptions, workers: int) -> List[str]:
    """Node roles that get the extra storage disk"""
    if opts.storage == "none":
        return []
    if opts.storage == "lvms":
        return ["ctlplane", "worker"]
    return ["worker"] if workers >= 3 else ["ctlplane"]


class OpenShiftInstaller:
    def __init__(self) -> None:
        # Live install view per cluster id (Assisted Service snapshot, cluster operators), for the UI
        self._status: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- helpers

    @property
    def svc(self):
        from app.services.cluster_service import cluster_service
        return cluster_service

    def _set_live(self, cluster_id: int, **values: Any) -> None:
        with self._lock:
            self._status.setdefault(cluster_id, {}).update(values)

    def live(self, cluster_id: int) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status.get(cluster_id, {}))

    @staticmethod
    def options(cluster: Cluster) -> OpenShiftOptions:
        return OpenShiftOptions.model_validate((cluster.spec or {}).get("openshift") or {})

    @staticmethod
    def zone(cluster: Cluster) -> str:
        return f"{cluster.name}.{cluster.domain}"

    def console_url(self, cluster: Cluster) -> str:
        return f"https://console-openshift-console.apps.{self.zone(cluster)}"

    def _update_spec(self, db: Session, cluster: Cluster, **values: Any) -> None:
        cluster.spec = {**(cluster.spec or {}), **values}
        db.commit()
        self.save_spec(cluster)

    @staticmethod
    def save_spec(cluster: Cluster) -> None:
        """Keep the cluster's spec (options, add-on states, MetalLB pool) next to its install files, so
        a cluster rebuilt from libvirt metadata (other DB, lost DB) gets it back. No secrets in it."""
        from app.services.cluster_service import SECRET_SPEC_KEYS
        spec = {k: v for k, v in (cluster.spec or {}).items() if k not in SECRET_SPEC_KEYS}
        path = openshift_service.cluster_dir(cluster.name) / "spec.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(spec, f, indent=1)

    @staticmethod
    def load_spec(name: str) -> Optional[Dict[str, Any]]:
        path = openshift_service.root / "clusters" / name / "spec.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _set_addon(self, db: Session, cluster: Cluster, kind: str, name: str, state: str,
                   message: Optional[str] = None) -> None:
        addons = [dict(a) for a in (cluster.spec or {}).get("addons") or []]
        entry = next((a for a in addons if a["kind"] == kind and a["name"] == name), None)
        if entry is None:
            entry = {"kind": kind, "name": name}
            addons.append(entry)
        entry.update(state=state, message=message)
        self._update_spec(db, cluster, addons=addons)
        self.svc._publish(cluster)

    # ------------------------------------------------------------- creating

    def create(self, db: Session, data: ClusterCreate) -> Cluster:
        opts = data.openshift or OpenShiftOptions()
        openshift_service.pull_secret()  # raises when missing
        counts = topology_counts(opts, data.workers)
        if opts.storage == "odf" and counts["masters"] + counts["workers"] < 3:
            raise ValueError("ODF needs 3 nodes: use LVMS on single-node OpenShift")
        if opts.storage == "odf" and opts.topology == "ha" and 0 < counts["workers"] < 3:
            raise ValueError("ODF on an HA cluster runs on the workers: 3 workers at least (or use compact)")
        try:
            ipaddress.IPv4Network(opts.sriov.ipam_range, strict=False)
        except ValueError as e:
            raise ValueError(f"SR-IOV IPAM range: {e}")

        sizes = self._sizes(data, opts, counts)
        version = openshift_service.resolve_version(opts.channel, opts.version)
        names = self.svc._node_names(data.name, counts["masters"], counts["workers"])

        def check(gspec) -> None:
            used = {lb.port: lb.name for lb in gspec.load_balancers}
            for port in LB_PORTS.values():
                if port in used:
                    raise ValueError(f"Lab group {gspec.name}: router port {port} is used by load balancer "
                                     f"{used[port]} (one OpenShift cluster per group, and no kubeadm cluster)")
            openshift_service.check_cidr(gspec.cidr)

        group, gspec, owned, node_subnet = self.svc._resolve_group(db, data, names, RESERVED_CIDRS, check)
        if opts.disconnected:
            # the router becomes the lab's bastion: mirror registry (more RAM + a registry disk)
            if not version.startswith("4."):
                raise ValueError("Disconnected installs: OpenShift 4.x releases only")
            gspec.router.registry.enabled = True
        if gspec.router.wireguard and gspec.router.wireguard.subnet:
            for reserved in RESERVED_CIDRS:
                if ipaddress.IPv4Network(gspec.router.wireguard.subnet).overlaps(ipaddress.IPv4Network(reserved)):
                    raise ValueError(f"The group's WireGuard subnet overlaps {reserved}, used inside OpenShift")
        self._check_budget(db, sizes, counts, opts, self._registry_extra(gspec, owned) if opts.disconnected else 0)

        task = None
        if group is None:
            group, task = group_service.create_group(db, gspec)

        spec = data.model_dump()
        spec.pop("network", None)
        spec["openshift"] = {**opts.model_dump(), "version": version}
        spec.update(ctlplanes=counts["masters"], workers=counts["workers"], ctlplane=sizes["ctlplane"],
                    worker=sizes["worker"], cidr=str(node_subnet), pod_cidr=POD_CIDR, service_cidr=SERVICE_CIDR,
                    api_port=LB_PORTS["api"], group_task_id=task.id if task else None, addons=[],
                    image=f"OpenShift {version}")
        cluster = Cluster(
            name=data.name, type="openshift", version=version, network=group_network_name(group.name),
            network_owned=False, group_id=group.id, group_owned=owned, domain=gspec.domain, spec=spec,
            status="provisioning",
            status_message="Creating the lab group (router first boot)" if owned else "Queued",
        )
        db.add(cluster)
        db.commit()
        db.refresh(cluster)
        self.save_spec(cluster)
        self.svc._publish(cluster)
        topo = {"sno": "single node", "compact": "compact (3 masters)",
                "ha": f"3 masters + {counts['workers']} workers"}[opts.topology]
        where = f"new lab group {group.name}" if owned else f"lab group {group.name}"
        if opts.disconnected:
            where += ", disconnected (mirror registry on the router)"
        task = task_service.start(db, TaskCreate(
            name=f"Create OpenShift cluster {data.name}", type="cluster_create", target_type="cluster",
            target_id=cluster.id, target_name=data.name, description=f"OpenShift {version}, {topo}, {where}",
        ), self.provision, cluster.id)
        cluster.task_id = task.id
        db.commit()
        return cluster

    @staticmethod
    def _sizes(data: ClusterCreate, opts: OpenShiftOptions, counts: Dict[str, int]) -> Dict[str, Dict[str, int]]:
        """Node sizes: the request's when given (checked against the Assisted minimums), else defaults
        (+ the ODF overhead on storage nodes)"""
        roles = storage_roles(opts, counts["workers"]) if opts.storage == "odf" else []
        result = {}
        for role, kind in (("ctlplane", "sno" if opts.topology == "sno" else "master"), ("worker", "worker")):
            given = role in data.model_fields_set
            res = dict(getattr(data, role).model_dump()) if given else dict(DEFAULTS[kind])
            if not given and role in roles:
                res["memory"] += ODF_EXTRA[opts.odf_profile]["memory"]
                res["vcpu"] += ODF_EXTRA[opts.odf_profile]["vcpu"]
            if role == "ctlplane" or counts["workers"]:
                low = [f"{k} {res[k]} < {v}" for k, v in MINIMUMS[kind].items() if res[k] < v]
                if low:
                    unit = {"memory": "MiB", "vcpu": "vCPU", "disk_size": "GiB"}
                    raise ValueError(f"{'Master' if role == 'ctlplane' else 'Worker'} too small for OpenShift: "
                                     + ", ".join(f"{k} {res[k]} {unit[k]} (minimum {MINIMUMS[kind][k]})"
                                                 for k in MINIMUMS[kind] if res[k] < MINIMUMS[kind][k]))
            result[role] = res
        return result

    @staticmethod
    def _registry_extra(gspec, owned: bool) -> int:
        """MiB the router needs on top of what it uses now to run the mirror registry"""
        want = gspec.router.effective_memory()  # registry enabled in gspec by the caller
        if owned:
            return want
        live = libvirt_client.get_vm(router_vm_name(gspec.name)) or {}
        current = live.get("max_memory", 0) // 1024 if live.get("state") == "running" else 0
        return max(0, want - current)

    @staticmethod
    def _check_budget(db: Session, sizes: Dict[str, Dict[str, int]], counts: Dict[str, int],
                      opts: OpenShiftOptions, extra: int = 0) -> None:
        """Refuse a cluster whose RAM can't fit next to the running VMs (no overcommit on purpose:
        an OOM-killed master corrupts etcd). extra: MiB more for the router (mirror registry)."""
        need = (sizes["ctlplane"]["memory"] * counts["masters"] + sizes["worker"]["memory"] * counts["workers"]
                + extra)
        try:
            with open("/proc/meminfo") as f:
                total = next(int(line.split()[1]) // 1024 for line in f if line.startswith("MemTotal:"))
        except (OSError, StopIteration, ValueError):
            return
        running = sum(vm.get("max_memory", 0) // 1024 for vm in libvirt_client.list_vms() if vm.get("state") == "running")
        free = total - running - 4096  # keep 4 GiB for the host
        if need > free:
            raise ValueError(f"Not enough memory: the cluster needs {need // 1024} GiB"
                             + (f" (including {extra // 1024} GiB more for the router's mirror registry)" if extra else "")
                             + ", the host has "
                             f"{total // 1024} GiB with {running // 1024} GiB used by running VMs "
                             "(stop some VMs or shrink the nodes)")

    # ------------------------------------------------------------ provision

    def provision(self, db: Session, task: Task, cluster_id: int) -> Dict[str, Any]:
        svc = self.svc

        def body(cluster: Cluster) -> Dict[str, Any]:
            spec = cluster.spec
            opts = self.options(cluster)
            version = cluster.version
            self._set_live(cluster.id, phase="preparing", assisted_status=None, progress=None, hosts=[],
                           cluster_operators=[])
            if cluster.group_owned:
                svc._wait_group(db, task, cluster)
            svc._check(task, cluster, db, 21, "Starting the lab group's router")
            network = GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))
            network.ensure()

            bin_end = 24 if opts.disconnected else 30

            def bin_progress(message: str, pct: int) -> None:
                svc._check(task, cluster, db, 22 + pct * (bin_end - 22) // 100, message)
            openshift_service.ensure_binaries(version, bin_progress, lambda: task_service.is_cancelled(task.id))
            if opts.disconnected:
                self._mirror(db, task, cluster, opts, network, (24, 30))

            svc._check(task, cluster, db, 30, "Reserving node addresses, DNS records and load balancers"
                       + (", blocking the group's egress" if opts.disconnected else ""))
            nodes = self._allocate(db, cluster, network, opts)

            svc._check(task, cluster, db, 31, "Building the agent ISO (openshift-install agent create image)")
            iso = self._create_image(db, task, cluster, nodes, opts)

            svc._check(task, cluster, db, 40, "Creating the node VMs")
            self._create_vms(db, cluster, nodes, opts, iso)
            for node in sorted(nodes, key=lambda n: n.role != "ctlplane"):
                libvirt_client.start_vm(node.name)
            self._set_live(cluster.id, phase="booting")
            return self._finish(db, task, cluster, nodes, iso)

        try:
            return svc._guarded(db, task, cluster_id, body)
        except Exception:
            self._set_live(cluster_id, phase="error")
            raise

    def _finish(self, db: Session, task: Task, cluster: Cluster, nodes: List[ClusterNode],
                iso: Optional[str]) -> Dict[str, Any]:
        """Once the node VMs boot: follow the install, then eject, disable updates, add-ons"""
        svc = self.svc
        opts = self.options(cluster)
        self._wait_install(db, task, cluster, nodes)

        svc._check(task, cluster, db, 93, "Ejecting the agent ISO")
        self._eject(cluster, iso)
        if opts.disconnected:
            svc._check(task, cluster, db, 93, "Switching to the mirror: IDMS / ITMS, mirrored catalog, default sources off")
            self._apply_mirror_resources(db, task, cluster)
        if opts.disable_updates:
            openshift_service.oc(cluster.name, cluster.version, ["patch", "clusterversion", "version", "--type",
                                                                  "merge", "-p", '{"spec":{"channel":""}}'], 60)
        self._run_addons(db, task, cluster, opts, initial=True)
        self._set_live(cluster.id, phase="ready")
        svc._set_status(db, cluster, "ready", None)
        return {"version": cluster.version, "console": self.console_url(cluster),
                "api_endpoint": f"https://{cluster.api_ip}:{LB_PORTS['api']}"}

    def resumable(self, cluster: Cluster) -> bool:
        """An install interrupted by a server restart once its node VMs were created and booted from the
        agent ISO: the install goes on in the nodes by itself, only the follow-up has to run again"""
        return cluster.status == "provisioning" and bool((cluster.spec or {}).get("iso_path")) and bool(cluster.nodes)

    def resume(self, db: Session, task: Task, cluster_id: int) -> Dict[str, Any]:
        svc = self.svc

        def body(cluster: Cluster) -> Dict[str, Any]:
            self._set_live(cluster.id, phase="booting", assisted_status=None, progress=None, hosts=[],
                           cluster_operators=[])
            missing = [n.name for n in cluster.nodes if libvirt_client.get_vm(n.name) is None]
            if missing:
                raise RuntimeError(f"Node VMs are gone: {', '.join(missing)}: delete the cluster and create it again")
            if cluster.group_id:
                GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned)).ensure()
            for node in cluster.nodes:
                live = libvirt_client.get_vm(node.name)
                if live and live["state"] == "shutoff":
                    libvirt_client.start_vm(node.name)
            svc._check(task, cluster, db, 45, "Resuming after a server restart: following the install")
            return self._finish(db, task, cluster, list(cluster.nodes), (cluster.spec or {}).get("iso_path"))

        try:
            return svc._guarded(db, task, cluster_id, body)
        except Exception:
            self._set_live(cluster_id, phase="error")
            raise

    def _allocate(self, db: Session, cluster: Cluster, network: GroupClusterNetwork,
                  opts: OpenShiftOptions) -> List[ClusterNode]:
        """Node rows (IP + MAC), reservations, DNS records, load balancers, MetalLB pool: one router push"""
        from app.services.cluster_service import _random_mac
        spec = cluster.spec
        zone = self.zone(cluster)
        if not cluster.nodes:
            names = self.svc._node_names(cluster.name, spec["ctlplanes"], spec["workers"])
            roles = ["ctlplane"] * spec["ctlplanes"] + ["worker"] * spec["workers"]
            taken: List[str] = []
            for name, role in zip(names, roles):
                ip = network.allocate_ip(taken)
                taken.append(ip)
                cluster.nodes.append(ClusterNode(name=name, role=role, ip=ip, mac=_random_mac()))
            db.commit()
        nodes = list(cluster.nodes)
        masters = [n.ip for n in nodes if n.role == "ctlplane"]
        workers = [n.ip for n in nodes if n.role == "worker"]
        ingress = workers or masters
        gspec = network.spec()
        router_ip = gspec.router.ip
        for node in nodes:
            network.reserve(node.name, node.mac, node.ip)
            network.publish(node.ip, [f"{node.name}.{zone}", node.name])
        network.publish(router_ip, [f"api.{zone}", f"api-int.{zone}", f"*.apps.{zone}"])
        network.publish_lb(f"{cluster.name}-api", LB_PORTS["api"], [f"{ip}:6443" for ip in masters])
        network.publish_lb(f"{cluster.name}-mcs", LB_PORTS["mcs"], [f"{ip}:22623" for ip in masters])
        network.publish_lb(f"{cluster.name}-http", LB_PORTS["http"], [f"{ip}:80" for ip in ingress])
        network.publish_lb(f"{cluster.name}-https", LB_PORTS["https"], [f"{ip}:443" for ip in ingress])
        if opts.metallb.enabled and not opts.metallb.pool:
            pool = self._assign_pool(network, gspec, opts.metallb.mode, opts.metallb.addresses, [n.ip for n in nodes],
                                     cluster.name)
            self._update_spec(db, cluster, openshift={**spec["openshift"], "metallb": {
                **spec["openshift"]["metallb"], "pool": pool}})
        if opts.disconnected:
            # before the nodes boot: everything they pull comes from the mirror (the router keeps its access)
            os_spec = cluster.spec.get("openshift") or {}
            if "egress_before" not in os_spec:
                self._update_spec(db, cluster, openshift={**os_spec, "egress_before": gspec.router.egress.mode})
            network.set_egress("blocked")
        network.commit()
        if not network.uplink_ip():
            raise RuntimeError(f"The router of group {network.group_name} has no uplink address: the host can't reach the API")
        cluster.api_ip = network.uplink_ip()
        db.commit()
        return nodes

    def _assign_pool(self, network: GroupClusterNetwork, gspec, mode: str, size: int, node_ips: List[str],
                     cluster_name: str) -> str:
        """MetalLB pool, recorded in the group spec (queued on `network`, applied by its commit()):
        l2 = a range of the group network kept free; bgp = a /27 outside it, a BGP announce range of the
        router (BGP enabled on the router if needed). The other mode's entry is dropped."""
        name = f"{cluster_name}-metallb"
        if mode == "bgp":
            pool = network.free_bgp_range()
            network.remove_address_pool(name)
            network.set_bgp_range(name, pool)
            return pool
        pool = self._pick_pool(gspec, size, node_ips)
        start, end = pool.split("-")
        network.remove_bgp_range(name)
        network.set_address_pool(name, start, end)
        return pool

    @staticmethod
    def _pick_pool(gspec, size: int, node_ips: List[str]) -> str:
        taken = [gspec.router.ip] + node_ips + [m.ip for m in gspec.members if m.ip]
        taken += [r.ip for r in gspec.reservations] + [h.ip for h in gspec.dhcp_hosts]
        taken += [r.a for r in gspec.router.dns.records if r.a]
        for p in gspec.address_pools:
            a, b = ipaddress.IPv4Address(p.start), ipaddress.IPv4Address(p.end)
            taken += [str(ipaddress.IPv4Address(i)) for i in range(int(a), int(b) + 1)]
        return metallb_pool(gspec.cidr, gspec.dhcp.end, size, taken)

    # ---------------------------------------------------- disconnected (mirror)

    def mirror_request(self, cluster: Cluster, opts: OpenShiftOptions, release: bool = True) -> MirrorRequest:
        """What a disconnected cluster needs in the group's registry: the release, the operator packages of
        its add-ons (+ their dependencies, default channels as the add-ons install them) and the images the
        add-ons pull outside the catalogs (hello demo)"""
        version = cluster.version
        minor = ".".join(version.split("-")[0].split(".")[:2])
        wanted: Dict[str, List[str]] = {}   # catalog source -> packages
        channels: Dict[str, Optional[str]] = {}
        if opts.storage == "lvms":
            wanted.setdefault("redhat-operators", []).append("lvms-operator")
        elif opts.storage == "odf":
            wanted.setdefault("redhat-operators", []).extend(["local-storage-operator", "odf-operator"])
        if opts.sriov.enabled:
            wanted.setdefault("redhat-operators", []).append("sriov-network-operator")
        if opts.metallb.enabled:
            wanted.setdefault("redhat-operators", []).append("metallb-operator")
        mirrored = {name: source for source, name in self.catalog_sources(cluster).items()}
        for op in opts.operators:
            # picked from the cluster's (mirrored) catalog: cs-redhat-operator-index-v4-20 -> redhat-operators
            names = wanted.setdefault(mirrored.get(op.source, op.source), [])
            if op.name not in names:
                names.append(op.name)
            channels[op.name] = op.channel
        catalogs: List[OperatorCatalog] = []
        for source, names in wanted.items():
            index = openshift_service.catalog_index(source, minor)
            if index is None:
                raise ValueError(f"Operator source {source}: not a default catalog, can't be mirrored")
            if source == "redhat-operators":
                names = openshift_service.operator_closure(minor, names)
            catalogs.append(OperatorCatalog(catalog=index, packages=[
                OperatorPackage(name=n, channel=channels.get(n)) for n in names]))
        images = [DEMO_IMAGE] if opts.metallb.enabled and opts.metallb.demo else []
        return MirrorRequest(openshift_version=version if release else None, operators=catalogs,
                             additional_images=images)

    def _mirror_file(self, cluster: Cluster):
        return openshift_service.cluster_dir(cluster.name) / "mirror.json"

    def load_mirror(self, cluster: Cluster) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self._mirror_file(cluster).read_text())
        except (OSError, ValueError):
            return None

    def mirror_pull_secret(self, cluster: Cluster) -> str:
        """The cluster's pull secret when disconnected: the mirror registry's credentials only"""
        mirror = self.load_mirror(cluster) or {}
        return json.dumps(mirror.get("pull_secret_fragment") or {"auths": {}}, separators=(",", ":"))

    @staticmethod
    def install_mirror(mirror: Dict[str, Any]) -> Dict[str, Any]:
        """install-config mirror settings. Every mirror on registry.<domain> also gets its <uplink_ip>
        twin, tried second: `openshift-install agent create image` runs on the host, which can't resolve
        registry.<domain> but reaches the router's uplink address (the installer's oc calls use
        --insecure=true, so the CA isn't needed there); the nodes resolve the first one."""
        host, uplink = mirror["registry_host"], mirror.get("uplink_registry_host")
        sources = []
        for entry in mirror.get("image_digest_sources") or []:
            mirrors = list(entry["mirrors"])
            for m in entry["mirrors"]:
                if uplink and m.startswith(host + "/"):
                    twin = uplink + m[len(host):]
                    if twin not in mirrors:
                        mirrors.append(twin)
            sources.append({"source": entry["source"], "mirrors": mirrors})
        return {"image_digest_sources": sources, "ca_pem": mirror["ca_pem"]}

    def _mirror(self, db: Session, task: Task, cluster: Cluster, opts: OpenShiftOptions,
                network: GroupClusterNetwork, window: tuple) -> Dict[str, Any]:
        """Registry enabled on the group router (owned change; the setup grows the router and installs Quay),
        then the release + add-on operators + images mirrored into it (idempotent: fast when done before).
        Saves the results (incl. the registry credentials: 0600, install dir) for the ISO and after install."""
        svc = self.svc
        if not network.spec().router.registry.enabled:
            svc._check(task, cluster, db, window[0], "Enabling the mirror registry on the group router")
            network.enable_registry()
            network.commit()
        svc._check(task, cluster, db, window[0], "Mirror registry: setting up the router (Quay), then oc-mirror "
                                                 "(first time: ~20+ GB, 30-90 min)")
        group = db.query(Group).filter(Group.id == cluster.group_id).first()
        request = self.mirror_request(cluster, opts)
        self._update_spec(db, cluster, openshift={**cluster.spec["openshift"],
                                                  "mirror_request": request.model_dump(mode="json")})
        try:
            result = registry_service.ensure_mirrored(db, group, request, task, window=window)
        except RuntimeError as e:
            if str(e) == "Cancelled":
                from app.services.cluster_service import Cancelled
                raise Cancelled()
            raise
        return self._save_mirror(db, cluster, result)

    def _save_mirror(self, db: Session, cluster: Cluster, result: Any) -> Dict[str, Any]:
        data = result.model_dump(mode="json")
        fd = os.open(self._mirror_file(cluster), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        # what the API may show (no credentials)
        self._update_spec(db, cluster, openshift={**cluster.spec["openshift"], "mirror": {
            "registry": result.registry_host, "uplink_registry": result.uplink_registry_host,
            "catalog_sources": result.catalog_sources}})
        return data

    def catalog_sources(self, cluster: Cluster) -> Dict[str, str]:
        """OperatorHub source name -> mirrored CatalogSource (e.g. redhat-operators -> cs-redhat-operator-index-v4-20)"""
        mirror = ((cluster.spec or {}).get("openshift") or {}).get("mirror") or {}
        result: Dict[str, str] = {}
        for index, name in (mirror.get("catalog_sources") or {}).items():
            base = index.rsplit("/", 1)[-1].split(":", 1)[0]  # redhat-operator-index
            source = next((s for s, i in CATALOG_INDEXES.items() if i == base), None)
            if source:
                result[source] = name
        return result

    def _apply_mirror_resources(self, db: Session, task: Task, cluster: Cluster) -> None:
        """oc-mirror's cluster resources (IDMS, ITMS, CatalogSources, release signatures), default
        OperatorHub sources off, then wait for the mirrored catalogs to serve. Idempotent."""
        svc = self.svc
        mirror = self.load_mirror(cluster)
        if mirror is None:
            raise RuntimeError("The mirror results are missing (mirror.json in the install dir)")
        runner = self._runner(db, task, cluster)
        skipped = []
        for doc in mirror.get("cluster_resources") or []:
            obj = yaml.safe_load(doc)
            if not isinstance(obj, dict) or not obj.get("kind"):
                continue
            try:
                runner.apply([obj])
            except RuntimeError as e:
                if "no matches for kind" in str(e) or "ensure CRDs are installed" in str(e):
                    skipped.append(obj["kind"])  # e.g. OLM v1 ClusterCatalog on an older release
                    continue
                runner.apply_retry([obj], f"{obj['kind']} {obj.get('metadata', {}).get('name')}", timeout=180)
        if skipped:
            logger.info(f"{cluster.name}: skipped mirror resources {', '.join(sorted(set(skipped)))} (no such kind)")
        openshift_service.oc(cluster.name, cluster.version, ["patch", "operatorhub", "cluster", "--type", "merge",
                                                             "-p", '{"spec":{"disableAllDefaultSources":true}}'], 60)
        names = sorted(set((mirror.get("catalog_sources") or {}).values()))
        deadline = time.monotonic() + CATALOG_TIMEOUT
        while names:
            states = {}
            for name in names:
                out = openshift_service.oc(cluster.name, cluster.version, [
                    "get", "catalogsource", name, "-n", "openshift-marketplace",
                    "-o", "jsonpath={.status.connectionState.lastObservedState}"], 30)
                states[name] = out["stdout"].strip() if out["exitcode"] == 0 else "missing"
            if all(s == "READY" for s in states.values()):
                return
            if time.monotonic() > deadline:
                raise TimeoutError("Mirrored catalog not ready after "
                                   f"{CATALOG_TIMEOUT // 60} min: " + ", ".join(f"{n}: {s or 'pending'}" for n, s in states.items()))
            svc._check(task, cluster, db, None, "Waiting for the mirrored catalog: "
                       + ", ".join(f"{n} {s or 'pending'}" for n, s in states.items()))
            svc._sleep(task, 10)

    def _mirror_more(self, db: Session, task: Task, cluster: Cluster, opts: OpenShiftOptions) -> None:
        """Day 2 on a disconnected cluster: mirror what the new add-on needs. The request repeats every
        operator of the cluster (oc-mirror pushes one filtered catalog per index: a smaller request would
        drop the packages mirrored before), without the release (already there)."""
        group = db.query(Group).filter(Group.id == cluster.group_id).first()
        if group is None:
            raise ValueError("The cluster's lab group no longer exists")
        request = self.mirror_request(cluster, opts, release=False)
        if not (request.operators or request.additional_images):
            return
        self.svc._check(task, cluster, db, 5, "Mirroring the add-on into the group's registry (oc-mirror on the router)")
        try:
            result = registry_service.ensure_mirrored(db, group, request, task, window=(5, 40))
        except RuntimeError as e:
            if str(e) == "Cancelled":
                from app.services.cluster_service import Cancelled
                raise Cancelled()
            raise
        previous = self.load_mirror(cluster) or {}
        data = result.model_dump(mode="json")
        # keep the release's digest sources (install-time) next to the new run's resources
        data["image_digest_sources"] = previous.get("image_digest_sources") or data["image_digest_sources"]
        catalogs = {**(previous.get("catalog_sources") or {}), **result.catalog_sources}
        result.catalog_sources = catalogs
        data["catalog_sources"] = catalogs
        fd = os.open(self._mirror_file(cluster), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        self._update_spec(db, cluster, openshift={**cluster.spec["openshift"],
                                                  "mirror_request_day2": request.model_dump(mode="json"),
                                                  "mirror": {**((cluster.spec["openshift"]).get("mirror") or {}),
                                                             "catalog_sources": catalogs}})
        self.svc._check(task, cluster, db, 40, "Updating the cluster's mirrored catalog")
        self._apply_mirror_resources(db, task, cluster)

    def release_group(self, cluster: Cluster, network: GroupClusterNetwork) -> None:
        """Cluster deleted from a group it doesn't own: put the group's egress back as it was (queued on
        `network`). The registry and its content stay (the group's, reusable by the next cluster)."""
        os_spec = (cluster.spec or {}).get("openshift") or {}
        before = os_spec.get("egress_before")
        if os_spec.get("disconnected") and before and before != "blocked":
            network.set_egress(before)

    # -------------------------------------------------------------- the ISO

    def _create_image(self, db: Session, task: Task, cluster: Cluster, nodes: List[ClusterNode],
                      opts: OpenShiftOptions) -> str:
        svc = self.svc
        spec = cluster.spec
        workdir = openshift_service.cluster_dir(cluster.name)
        # A retry starts from scratch (the installer refuses a dir with a previous state), keeping the SSH key
        for entry in workdir.iterdir():
            if entry.name.startswith("id_ed25519") or entry.name == "spec.json":
                continue
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        # the app's key + the user's (all authorized for `core`)
        ssh_key = "\n".join([openshift_service.ssh_keypair(workdir)]
                            + [k.strip() for k in spec.get("ssh_keys") or [] if k.strip()])
        network = GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))
        first = next(n for n in nodes if n.role == "ctlplane")
        mirror = self.load_mirror(cluster) if opts.disconnected else None
        if opts.disconnected and mirror is None:
            raise RuntimeError("The mirror results are missing: delete the cluster and create it again")
        install = openshift_service.install_config(
            cluster.name, cluster.domain, spec["cidr"], spec["ctlplanes"], spec["workers"],
            # disconnected: the registry's credentials only (no quay.io / cloud.openshift.com: no telemetry)
            self.mirror_pull_secret(cluster) if mirror else openshift_service.pull_secret(),
            ssh_key, self.install_mirror(mirror) if mirror else None)
        agent = openshift_service.agent_config(
            cluster.name, first.ip, network.gateway(),  # NTP: chrony on the router
            [{"name": n.name, "role": "master" if n.role == "ctlplane" else "worker", "mac": n.mac} for n in nodes])
        configs = {"install-config.yaml": install, "agent-config.yaml": agent}
        for name, data in configs.items():
            text = yaml.dump(data, Dumper=_BlockDumper, sort_keys=False)
            for path in (workdir / name, workdir / f"{name}.orig"):  # the installer consumes the first
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(text)
        manifests = self._manifests(cluster, opts)
        if manifests:
            (workdir / "openshift").mkdir(exist_ok=True)
            for name, data in manifests.items():
                (workdir / "openshift" / name).write_text(yaml.safe_dump(data, sort_keys=False))

        binary = openshift_service.bin_dir(cluster.version) / "openshift-install"
        cache = openshift_service.root / "cache"
        cache.mkdir(exist_ok=True)
        env = {"PATH": f"{openshift_service.bin_dir(cluster.version)}:{os.environ.get('PATH', '/usr/bin:/bin')}",
               "HOME": str(openshift_service.root), "XDG_CACHE_HOME": str(cache)}
        log_path = workdir / "create-image.log"
        with open(log_path, "w") as log:
            proc = subprocess.Popen([str(binary), "agent", "create", "image", "--dir", str(workdir),
                                     "--log-level", "info"], stdout=log, stderr=subprocess.STDOUT, env=env)
            deadline = time.monotonic() + IMAGE_TIMEOUT
            try:
                while proc.poll() is None:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"agent create image did not finish in {IMAGE_TIMEOUT // 60} min")
                    last = self._last_log_line(log_path)
                    svc._check(task, cluster, db, 32 if "Extract" not in last else 34,
                               f"Building the agent ISO: {last}" if last else None)
                    svc._sleep(task, 3)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        if proc.returncode != 0:
            tail = log_path.read_text()[-1500:]
            raise RuntimeError(f"openshift-install agent create image failed:\n{tail}")

        kubeconfig = (workdir / "auth" / "kubeconfig").read_text()
        host_config = openshift_service.kubeconfig_for_host(
            kubeconfig, cluster.name, f"https://{cluster.api_ip}:{LB_PORTS['api']}", f"api.{self.zone(cluster)}")
        fd = os.open(workdir / "host-kubeconfig", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(host_config)
        cluster.kubeconfig = host_config
        db.commit()

        svc._check(task, cluster, db, 36, "Uploading the agent ISO to the default pool")
        iso_file = workdir / "agent.x86_64.iso"
        pool_name = libvirt_client.ensure_pool(settings.DEFAULT_POOL_NAME, settings.DEFAULT_POOL_PATH).name()
        name = f"{cluster.name}-agent.iso"
        libvirt_client.delete_volume(pool_name, name)
        with open(iso_file, "rb") as f:
            path = libvirt_client.upload_volume(pool_name, name, iso_file.stat().st_size, f)
        iso_file.unlink()  # the copy in the pool is the one the VMs use
        self._update_spec(db, cluster, iso_path=path)
        return path

    @staticmethod
    def _last_log_line(path) -> str:
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 2000))
                lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
        except OSError:
            return ""
        if not lines:
            return ""
        line = lines[-1]
        if 'msg="' in line:
            line = line.split('msg="', 1)[1].rstrip('"')
        return line[:160]

    def _manifests(self, cluster: Cluster, opts: OpenShiftOptions) -> Dict[str, Dict[str, Any]]:
        """Day-1 manifests (openshift/ dir of the install): what would otherwise need a reboot"""
        result: Dict[str, Dict[str, Any]] = {}
        if opts.sriov.enabled and opts.sriov.device_type == "vfio-pci":
            roles = ["master"] if (cluster.spec or {}).get("workers", 0) == 0 else ["worker"]
            for mc in AddonRunner.sriov_machineconfigs(roles):
                result[f"{mc['metadata']['name']}.yaml"] = mc
        return result

    # -------------------------------------------------------------- the VMs

    def _create_vms(self, db: Session, cluster: Cluster, nodes: List[ClusterNode], opts: OpenShiftOptions,
                    iso: str) -> None:
        from app.services.cluster_service import METADATA_KEY, METADATA_URI
        spec = cluster.spec
        group = db.query(Group).filter(Group.id == cluster.group_id).first()
        disks_for = storage_roles(opts, spec["workers"])
        pool_name = libvirt_client.ensure_pool(settings.DEFAULT_POOL_NAME, settings.DEFAULT_POOL_PATH).name()
        for node in nodes:
            if libvirt_client.get_vm(node.name) is not None:  # retry after a failure: start over
                libvirt_client.delete_vm(node.name, delete_disks=True)
            res = spec["ctlplane"] if node.role == "ctlplane" else spec["worker"]
            attrs = {"name": cluster.name, "type": "openshift", "role": node.role, "ip": node.ip,
                     "domain": cluster.domain, "network": cluster.network, "owned": "no",
                     "cidr": spec.get("cidr") or "", "version": cluster.version or "",
                     "group": group.name if group else "", "group_owned": "yes" if cluster.group_owned else "no",
                     "api_port": LB_PORTS["api"]}
            metadata = (f"<{METADATA_KEY}:cluster xmlns:{METADATA_KEY}={quoteattr(METADATA_URI)} "
                        + " ".join(f"{k}={quoteattr(str(v))}" for k, v in attrs.items()) + "/>")
            extra = []
            if opts.sriov.enabled:
                extra = [VMNicCreate(network=self._sriov_network(cluster), model="igb") for _ in range(opts.sriov.nics)]
            vm_service.create_vm(db, VMCreate(
                name=node.name, description=f"OpenShift cluster {cluster.name}: "
                                            f"{'master' if node.role == 'ctlplane' else 'worker'}",
                memory=res["memory"], vcpu=res["vcpu"], disk_size=res["disk_size"], iso_path=iso,
                network_name=cluster.network, mac_address=node.mac, os_type=f"OpenShift {cluster.version}",
                extra_nics=extra, iommu=opts.sriov.enabled, start=False,
            ), metadata_xml=metadata)
            if node.role in disks_for:
                path = libvirt_client.create_volume(pool_name, f"{node.name}-storage.qcow2",
                                                    opts.storage_disk_size * 1024 ** 3, "qcow2")
                libvirt_client.attach_disk(node.name, domain_xml.data_disk_xml(path, "vdb", "virtio", "qcow2",
                                                                                serial=STORAGE_SERIAL))

    @staticmethod
    def _sriov_network(cluster: Cluster) -> str:
        name = sriov_network_name(cluster.name)
        if not any(n["name"] == name for n in libvirt_client.list_networks()):
            libvirt_client.create_network(name, f"<network><name>{name}</name><bridge stp='on' delay='0'/></network>")
        return name

    def _eject(self, cluster: Cluster, iso: Optional[str]) -> None:
        for node in cluster.nodes:
            try:
                libvirt_client.set_cdrom(node.name, None)
            except (libvirt.libvirtError, ValueError) as e:
                logger.warning(f"Could not eject the agent ISO from {node.name}: {e}")
        if iso:
            libvirt_client.delete_volume_by_path(iso)

    # ------------------------------------------------------------- waiting

    def _router_exec(self, cluster: Cluster, script: str, timeout: float = 30) -> Dict[str, Any]:
        group = cluster.network[len("vmm-g-"):] if cluster.network.startswith("vmm-g-") else cluster.network
        return libvirt_client.agent_exec(router_vm_name(group), "/bin/sh", ["-c", script], timeout=timeout)

    def _watcher_token(self, cluster: Cluster) -> Optional[str]:
        state = openshift_service.cluster_dir(cluster.name) / ".openshift_install_state.json"
        try:
            data = json.loads(state.read_text())
        except (OSError, ValueError):
            return None
        for key, value in data.items():
            if "AuthConfig" in key and isinstance(value, dict):
                return value.get("WatcherAuthToken") or value.get("UserAuthToken") or value.get("AgentAuthToken")
        return None

    def assisted(self, cluster: Cluster) -> Optional[Dict[str, Any]]:
        """The Assisted Service's view of the install (rendezvous host, until it reboots into the cluster)"""
        first = next((n for n in cluster.nodes if n.role == "ctlplane"), None)
        if first is None:
            return None
        token = self._watcher_token(cluster)
        header = f"-H {shlex.quote('Watcher-Authorization: ' + token)} " if token else ""
        url = f"http://{first.ip}:{ASSISTED_PORT}/api/assisted-install/v2/clusters?with_hosts=true"
        try:
            out = self._router_exec(cluster, f"curl -sf -m 8 {header}{shlex.quote(url)}", 20)
        except (libvirt.libvirtError, TimeoutError, ValueError):
            return None
        if out["exitcode"] != 0:
            return None
        try:
            clusters = json.loads(out["stdout"])
        except ValueError:
            return None
        if not clusters:
            return {"status": "waiting", "status_info": "Waiting for the nodes to register", "hosts": []}
        c = clusters[0]
        hosts = []
        for h in c.get("hosts") or []:
            inv_name = h.get("requested_hostname")
            if not inv_name:
                try:
                    inv_name = json.loads(h.get("inventory") or "{}").get("hostname")
                except ValueError:
                    inv_name = None
            progress = h.get("progress") or {}
            hosts.append({"name": inv_name or h.get("id", "")[:8], "role": h.get("role"), "status": h.get("status"),
                          "stage": progress.get("current_stage") or h.get("status_info"),
                          "progress": progress.get("installation_percentage")})
        return {"status": c.get("status"), "status_info": c.get("status_info"),
                "progress": (c.get("progress") or {}).get("total_percentage"), "hosts": sorted(hosts, key=lambda h: h["name"])}

    def cluster_operators(self, cluster: Cluster) -> Optional[List[Dict[str, Any]]]:
        try:
            data = openshift_service.oc_json(cluster.name, cluster.version, ["get", "clusteroperators"], 30)
        except (RuntimeError, ValueError):
            return None
        result = []
        for item in data.get("items", []):
            conds = {c.get("type"): c for c in (item.get("status") or {}).get("conditions") or []}

            def flag(name: str) -> Optional[bool]:
                c = conds.get(name)
                return None if c is None else c.get("status") == "True"
            message = next((conds[t].get("message") for t in ("Degraded", "Progressing", "Available")
                            if t in conds and conds[t].get("status") == ("False" if t == "Available" else "True")
                            and conds[t].get("message")), None)
            versions = (item.get("status") or {}).get("versions") or []
            result.append({"name": item["metadata"]["name"], "available": flag("Available"),
                           "progressing": flag("Progressing"), "degraded": flag("Degraded"),
                           "message": (message or "")[:300] or None,
                           "version": next((v.get("version") for v in versions if v.get("name") == "operator"), None)})
        return result

    def clusterversion(self, cluster: Cluster) -> Optional[Dict[str, Any]]:
        try:
            data = openshift_service.oc_json(cluster.name, cluster.version, ["get", "clusterversion", "version"], 30)
        except (RuntimeError, ValueError):
            return None
        conds = {c.get("type"): c for c in (data.get("status") or {}).get("conditions") or []}
        history = (data.get("status") or {}).get("history") or []
        return {
            "available": (conds.get("Available") or {}).get("status") == "True",
            "progressing": (conds.get("Progressing") or {}).get("status") == "True",
            "message": (conds.get("Progressing") or {}).get("message"),
            "version": history[0].get("version") if history else None,
            "completed": bool(history) and history[0].get("state") == "Completed",
        }

    def _wait_install(self, db: Session, task: Task, cluster: Cluster, nodes: List[ClusterNode]) -> None:
        svc = self.svc
        deadline = time.monotonic() + INSTALL_TIMEOUT
        api_seen = False
        assisted_seen = False
        last_csr = 0.0
        while True:
            info = self.assisted(cluster)
            if info is not None:
                assisted_seen = True
                hosts = info.get("hosts") or []
                pct = info.get("progress") or 0
                self._set_live(cluster.id, phase="installing" if info.get("status") not in ("waiting", "insufficient",
                                                                                          "ready", "pending-for-input")
                               else "booting", assisted_status=info.get("status"), assisted_info=info.get("status_info"),
                               progress=pct, hosts=hosts)
                if info.get("status") == "error":
                    raise RuntimeError(f"The installation failed: {info.get('status_info')}")
                stages = ", ".join(f"{h['name']}: {h['stage']}" for h in hosts if h.get("stage"))[:200]
                svc._check(task, cluster, db, 45 + pct * 35 // 100,
                           f"Installing ({info.get('status')}{', ' + str(pct) + ' %' if pct else ''})"
                           + (f" — {stages}" if stages else ""))
            cv = self.clusterversion(cluster) if (info is None or (info.get("progress") or 0) >= 60) else None
            if cv is not None:
                api_seen = True
                if time.monotonic() - last_csr > 60:
                    openshift_service.approve_csrs(cluster.name, cluster.version)
                    last_csr = time.monotonic()
                ops = self.cluster_operators(cluster) or []
                done = sum(1 for o in ops if o["available"] and not o["progressing"] and not o["degraded"])
                self._set_live(cluster.id, phase="finalizing", cluster_operators=ops, version=cv.get("version"))
                if cv["available"] and not cv["progressing"] and cv["completed"]:
                    return
                svc._check(task, cluster, db, 80 + 12 * done // max(len(ops), 1),
                           f"Cluster operators: {done}/{len(ops)} available"
                           + (f" — {cv['message'][:150]}" if cv.get("message") else ""))
            elif info is None and not api_seen:
                svc._check(task, cluster, db, None,
                           "Rebooted into the installed system: waiting for the API" if assisted_seen
                           else "Booting the nodes from the agent ISO")
            if time.monotonic() > deadline:
                raise TimeoutError(f"OpenShift was not installed after {INSTALL_TIMEOUT // 60} min")
            svc._sleep(task, 20)

    # -------------------------------------------------------------- add-ons

    def _runner(self, db: Session, task: Task, cluster: Cluster) -> AddonRunner:
        svc = self.svc
        return AddonRunner(cluster.name, cluster.version, lambda s: svc._sleep(task, s),
                           lambda m: svc._check(task, cluster, db, None, m), sources=self.catalog_sources(cluster))

    def _run_addons(self, db: Session, task: Task, cluster: Cluster, opts: OpenShiftOptions, initial: bool) -> None:
        svc = self.svc
        runner = self._runner(db, task, cluster)
        plan: List[tuple] = []
        if opts.storage != "none":
            plan.append(("storage", opts.storage))
        if opts.sriov.enabled:
            plan.append(("sriov", "sriov-network-operator"))
        if opts.metallb.enabled:
            plan.append(("metallb", "metallb-operator"))
            if opts.metallb.demo:
                plan.append(("metallb-demo", "hello"))
        managed = {"lvms-operator", "odf-operator", "local-storage-operator", "sriov-network-operator", "metallb-operator"}
        plan += [("operator", o.name) for o in opts.operators if o.name not in managed]
        for kind, name in plan:
            self._set_addon(db, cluster, kind, name, "pending")
        self._set_live(cluster.id, phase="addons")
        errors = []
        for i, (kind, name) in enumerate(plan):
            svc._check(task, cluster, db, 94 + 5 * i // max(len(plan), 1), f"Add-on {name}")
            self._set_addon(db, cluster, kind, name, "installing")
            try:
                self.run_addon(db, cluster, runner, kind, name, opts)
                self._set_addon(db, cluster, kind, name, "done")
            except Exception as e:
                from app.services.cluster_service import Cancelled
                if isinstance(e, Cancelled):
                    raise
                logger.exception(f"Add-on {name} on {cluster.name}")
                self._set_addon(db, cluster, kind, name, "error", str(e)[-500:])
                errors.append(f"{name}: {str(e)[-200:]}")
        if errors:
            # the cluster itself is fine: report, don't fail the whole install
            cluster.status_message = "Some add-ons failed: " + "; ".join(errors)
            db.commit()

    def run_addon(self, db: Session, cluster: Cluster, runner: AddonRunner, kind: str, name: str,
                  opts: OpenShiftOptions) -> None:
        nodes_by_role = {r: [n.name for n in cluster.nodes if n.role == r] for r in ("ctlplane", "worker")}
        if kind == "storage":
            if name == "lvms":
                runner.lvms()
            else:
                roles = storage_roles(opts, len(nodes_by_role["worker"]))
                runner.odf([n for r in roles for n in nodes_by_role[r]], opts.odf_profile)
        elif kind == "sriov":
            runner.sriov(opts.sriov.model_dump(), single_or_compact=not nodes_by_role["worker"])
        elif kind == "metallb":
            mlb = (cluster.spec.get("openshift") or {}).get("metallb", {})
            pool, mode = mlb.get("pool"), mlb.get("mode") or "l2"
            if not pool:
                raise ValueError("No MetalLB address pool assigned")
            bgp = None
            if mode == "bgp":
                gbgp = GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned)).spec().router.bgp
                if gbgp is None or not gbgp.enabled:
                    raise ValueError("BGP is not enabled on the group router")
                bgp = {"router_ip": GroupClusterNetwork(cluster.group_id, cluster.name, owned=False).gateway(),
                       "my_asn": gbgp.peer_asn or BGP_PEER_ASN, "peer_asn": gbgp.asn}
            runner.metallb(pool, mode, bgp)
            if mlb.get("service_ip"):  # day 2 (e.g. L2 -> BGP): the demo moves to the new pool
                self.run_addon(db, cluster, runner, "metallb-demo", "hello", opts)
        elif kind == "metallb-demo":
            mlb = (cluster.spec.get("openshift") or {}).get("metallb", {})
            old_ip = mlb.get("service_ip")
            ip = runner.metallb_demo(pool=mlb.get("pool"), mode=mlb.get("mode") or "l2")
            network = GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))
            gspec = network.spec()
            if old_ip and old_ip != ip:
                network.unpublish(old_ip)
            network.publish(ip, [f"hello.{gspec.domain}"])
            network.commit()
            os_spec = cluster.spec.get("openshift") or {}
            self._update_spec(db, cluster, openshift={**os_spec, "metallb": {**os_spec.get("metallb", {}),
                                                                               "demo": True, "service_ip": ip}})
        elif kind == "operator":
            req = next((o for o in opts.operators if o.name == name), None)
            ns = runner.install_operator(name, req.channel if req else None, req.source if req else None,
                                         req.namespace if req else None)
            runner.operator_cr(name, ns)
        else:
            raise ValueError(f"Unknown add-on kind {kind}")

    # ------------------------------------------------------ day-2 add-ons

    def add_addon(self, db: Session, cluster: Cluster, request: Any) -> Task:
        """POST /clusters/{id}/openshift/addons: install one add-on on a ready cluster"""
        if self.svc.to_dict(db, cluster)["status"] != "ready":
            raise ValueError("Start the cluster first")
        os_spec = dict(cluster.spec.get("openshift") or {})
        kind = request.kind
        if kind == "operator":
            if request.operator is None:
                raise ValueError("operator is required")
            ops = [o for o in os_spec.get("operators") or [] if o.get("name") != request.operator.name]
            os_spec["operators"] = ops + [request.operator.model_dump()]
            name = request.operator.name
        elif kind in ("lvms", "odf"):
            if os_spec.get("storage") not in (None, "none", kind):
                raise ValueError(f"The cluster already uses {os_spec['storage']} storage")
            workers = sum(1 for n in cluster.nodes if n.role == "worker")
            if kind == "odf" and len(cluster.nodes) < 3:
                raise ValueError("ODF needs 3 nodes")
            os_spec["storage"] = kind
            missing = [n.name for n in cluster.nodes
                       if n.role in storage_roles(OpenShiftOptions.model_validate(os_spec), workers)
                       and not self._has_storage_disk(n.name)]
            if missing:
                raise ValueError(f"{', '.join(missing)} have no storage disk: add a {os_spec.get('storage_disk_size', 100)} "
                                 f"GiB disk with serial '{STORAGE_SERIAL}' first, or create the cluster with storage")
            kind, name = "storage", kind
        elif kind == "sriov":
            if not all(self._has_igb(n.name) for n in cluster.nodes):
                raise ValueError("The nodes have no igb NIC: SR-IOV must be chosen when the cluster is created")
            os_spec["sriov"] = {**(request.sriov.model_dump() if request.sriov else os_spec.get("sriov") or {}),
                                "enabled": True}
            name = "sriov-network-operator"
        elif kind == "metallb":
            prev = os_spec.get("metallb") or {}
            mlb = {**prev, **(request.metallb.model_dump(exclude={"pool"}) if request.metallb else {}),
                   "enabled": True}
            mode = mlb.get("mode") or "l2"
            if (prev.get("mode") or "l2") != mode and prev.get("pool"):
                mlb["pool"] = None  # switching L2 <-> BGP: a pool of the other kind
            if not mlb.get("pool"):
                network = GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))
                mlb["pool"] = self._assign_pool(network, network.spec(), mode, int(mlb.get("addresses") or 16),
                                                [n.ip for n in cluster.nodes], cluster.name)
                network.commit()
            os_spec["metallb"] = mlb
            name = "metallb-operator"
        elif kind == "metallb-demo":
            if not (os_spec.get("metallb") or {}).get("pool"):
                raise ValueError("Enable MetalLB first")
            name = "hello"
        else:
            raise ValueError(f"Unknown add-on {kind}")
        self._update_spec(db, cluster, openshift=os_spec)

        def run(db: Session, task: Task, cluster_id: int) -> Dict[str, Any]:
            def body(cluster: Cluster) -> Dict[str, Any]:
                opts = self.options(cluster)
                self._set_addon(db, cluster, kind, name, "installing")
                try:
                    if opts.disconnected:  # the cluster can only pull from the group's mirror
                        wanted = opts.model_copy(deep=True)
                        if kind == "metallb-demo":
                            wanted.metallb.demo = True
                        self._mirror_more(db, task, cluster, wanted)
                    self.run_addon(db, cluster, self._runner(db, task, cluster), kind, name, opts)
                except Exception as e:
                    self._set_addon(db, cluster, kind, name, "error", str(e)[-500:])
                    raise
                self._set_addon(db, cluster, kind, name, "done")
                self.svc._set_status(db, cluster, "ready", None)
                return {"addon": name}
            return self.svc._guarded(db, task, cluster_id, body, failed_status="ready")
        self._set_addon(db, cluster, kind, name, "pending")
        return self.svc._run_task(db, cluster, "addon", f"Add {name} to", run)

    @staticmethod
    def _has_storage_disk(vm: str) -> bool:
        xml = libvirt_client.get_vm_xml(vm) or ""
        return f"<serial>{STORAGE_SERIAL}</serial>" in xml

    @staticmethod
    def _has_igb(vm: str) -> bool:
        xml = libvirt_client.get_vm_xml(vm) or ""
        return "model type='igb'" in xml or 'model type="igb"' in xml

    # ----------------------------------------------------------- lifecycle

    def start(self, db: Session, task: Task, cluster: Cluster) -> Dict[str, Any]:
        """Masters first; kubelets whose certificates rotated while off need their CSRs approved"""
        svc = self.svc
        svc._set_status(db, cluster, "starting", "Starting the lab group's router")
        GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned)).ensure()
        since = datetime.now(timezone.utc) - timedelta(seconds=5)
        for role in ("ctlplane", "worker"):
            for node in cluster.nodes:
                live = libvirt_client.get_vm(node.name)
                if node.role == role and live and live["state"] == "shutoff":
                    libvirt_client.start_vm(node.name)
        expected = {n.name for n in cluster.nodes}
        deadline = time.monotonic() + START_TIMEOUT
        last = "API not reachable yet"
        while True:
            openshift_service.approve_csrs(cluster.name, cluster.version)
            out = openshift_service.oc(cluster.name, cluster.version, ["get", "nodes", "-o", "json"], 30)
            ready: Dict[str, bool] = {}
            if out["exitcode"] == 0:
                from app.services.cluster_drivers import ClusterDriver
                ready = ClusterDriver.parse_ready(out["stdout"], since)
            else:
                last = (out["stderr"] or out["stdout"]).strip()[-200:] or last
            count = sum(1 for n in expected if ready.get(n))
            svc._check(task, cluster, db, 10 + 85 * count // max(len(expected), 1),
                       f"Waiting for nodes: {count}/{len(expected)} Ready")
            if count == len(expected):
                return {"nodes_ready": count}
            if time.monotonic() > deadline:
                raise TimeoutError(f"Only {count}/{len(expected)} nodes Ready after {START_TIMEOUT // 60} min ({last})")
            svc._sleep(task, 10)

    def delete_files(self, cluster: Cluster) -> None:
        iso = (cluster.spec or {}).get("iso_path")
        if iso:
            libvirt_client.delete_volume_by_path(iso)
        try:
            libvirt_client.delete_network(sriov_network_name(cluster.name))
        except libvirt.libvirtError as e:
            logger.warning(f"Could not delete the SR-IOV network of {cluster.name}: {e}")
        shutil.rmtree(openshift_service.root / "clusters" / cluster.name, ignore_errors=True)
        with self._lock:
            self._status.pop(cluster.id, None)

    # ---------------------------------------------------------------- views

    def install_status(self, db: Session, cluster: Cluster) -> Dict[str, Any]:
        live = self.live(cluster.id)
        status = self.svc.to_dict(db, cluster)["status"]
        addons = (cluster.spec or {}).get("addons") or []
        if status == "provisioning" or task_service.is_running(cluster.task_id):
            return {"phase": live.get("phase") or "preparing", "assisted_status": live.get("assisted_status"),
                    "assisted_info": live.get("assisted_info"), "progress": live.get("progress"),
                    "hosts": live.get("hosts") or [], "version": live.get("version") or cluster.version,
                    "cluster_operators": live.get("cluster_operators") or [], "addons": addons}
        if status == "stopped":
            return {"phase": "stopped", "version": cluster.version, "addons": addons}
        if status == "error" and not cluster.kubeconfig:
            return {"phase": "error", "version": cluster.version, "addons": addons,
                    "hosts": live.get("hosts") or [], "assisted_info": live.get("assisted_info")}
        ops = self.cluster_operators(cluster) if status in ("ready", "error") else None
        cv = self.clusterversion(cluster) if ops is not None else None
        return {"phase": "ready" if status == "ready" else status, "version": (cv or {}).get("version") or cluster.version,
                "cluster_operators": ops or [], "addons": addons, "hosts": []}

    def credentials(self, cluster: Cluster) -> Dict[str, Any]:
        path = openshift_service.cluster_dir(cluster.name) / "auth" / "kubeadmin-password"
        password = path.read_text().strip() if path.exists() else None
        return {"username": "kubeadmin", "password": password, "console_url": self.console_url(cluster)}

    def ssh_key(self, cluster: Cluster) -> str:
        path = openshift_service.cluster_dir(cluster.name) / "id_ed25519"
        if not path.exists():
            raise ValueError("This cluster has no SSH key")
        return path.read_text()

    def operators(self, cluster: Cluster) -> List[Dict[str, Any]]:
        try:
            subs = openshift_service.oc_json(cluster.name, cluster.version, ["get", "subscriptions.operators.coreos.com", "-A"], 30)
            csvs = openshift_service.oc_json(cluster.name, cluster.version, ["get", "csv", "-A"], 60)
        except (RuntimeError, ValueError) as e:
            raise ValueError(f"Cannot read the operators: {e}")
        phases = {(c["metadata"]["namespace"], c["metadata"]["name"]): c for c in csvs.get("items", [])}
        result = []
        for sub in subs.get("items", []):
            ns = sub["metadata"]["namespace"]
            csv_name = (sub.get("status") or {}).get("installedCSV") or (sub.get("status") or {}).get("currentCSV")
            csv = phases.get((ns, csv_name)) or {}
            result.append({"name": (sub.get("spec") or {}).get("name") or sub["metadata"]["name"], "namespace": ns,
                           "channel": (sub.get("spec") or {}).get("channel"), "source": (sub.get("spec") or {}).get("source"),
                           "csv": csv_name, "phase": (csv.get("status") or {}).get("phase"),
                           "version": (csv.get("spec") or {}).get("version")})
        return sorted(result, key=lambda o: o["name"])

    def packagemanifests(self, cluster: Cluster) -> List[Dict[str, Any]]:
        try:
            data = openshift_service.oc_json(cluster.name, cluster.version,
                                             ["get", "packagemanifests", "-n", "openshift-marketplace"], 120)
        except (RuntimeError, ValueError) as e:
            raise ValueError(f"Cannot read the catalogs: {e}")
        result = []
        for item in data.get("items", []):
            status = item.get("status") or {}
            channels = status.get("channels") or []
            default = next((c for c in channels if c.get("name") == status.get("defaultChannel")), channels[0] if channels else {})
            desc = default.get("currentCSVDesc") or {}
            modes = {m.get("type"): m.get("supported") for m in desc.get("installModes") or []}
            result.append({
                "name": item["metadata"]["name"], "display_name": desc.get("displayName"),
                "provider": (status.get("provider") or {}).get("name"), "source": status.get("catalogSource") or "",
                "default_channel": status.get("defaultChannel"), "channels": [c.get("name") for c in channels],
                "description": (desc.get("annotations") or {}).get("description") or (desc.get("description") or "")[:300],
                "suggested_namespace": (desc.get("annotations") or {}).get("operatorframework.io/suggested-namespace"),
                "all_namespaces_only": not modes.get("OwnNamespace") and bool(modes.get("AllNamespaces")),
            })
        return sorted(result, key=lambda p: (p["display_name"] or p["name"]).lower())

    def metallb_scenario(self, db: Session, cluster: Cluster) -> Dict[str, Any]:
        os_spec = (cluster.spec or {}).get("openshift") or {}
        mlb = os_spec.get("metallb") or {}
        group = db.query(Group).filter(Group.id == cluster.group_id).first()
        gspec = (group.spec or {}) if group else {}
        router = gspec.get("router") or {}
        wg = router.get("wireguard") or {}
        mode = mlb.get("mode") or "l2"
        result: Dict[str, Any] = {
            "enabled": bool(mlb.get("enabled")), "mode": mode, "pool": mlb.get("pool"), "service_ip": mlb.get("service_ip"),
            "hostname": f"hello.{gspec.get('domain')}" if mlb.get("service_ip") and gspec.get("domain") else None,
            "announcing_node": None, "endpoints": [], "router_ip": router.get("ip"), "group_cidr": gspec.get("cidr"),
            "wireguard": bool(wg.get("enabled")), "wireguard_port": wg.get("host_port") if wg.get("enabled") else None,
            "checks": [],
        }
        if not result["enabled"] or self.svc.to_dict(db, cluster)["status"] != "ready":
            return result
        runner = AddonRunner(cluster.name, cluster.version, time.sleep, lambda m: None)
        checks = result["checks"]
        try:
            ip = runner.service_ip()
        except (RuntimeError, ValueError):
            ip = None
        if ip:
            result["service_ip"] = ip
            result["hostname"] = f"hello.{gspec.get('domain')}"
        checks.append({"name": "Service got an IP from the pool", "ok": bool(ip),
                       "detail": f"{ip} in {mlb.get('pool')}" if ip else "no external IP (is the demo deployed?)"})
        if not ip:
            return result
        if mode == "bgp":
            self._bgp_checks(cluster, group, ip, result)
        else:
            result["announcing_node"] = runner.announcing_node()
            checks.append({"name": "A node announces it (L2 / ARP)", "ok": bool(result["announcing_node"]),
                           "detail": result["announcing_node"] or "no speaker announces the IP yet"})
        result["endpoints"] = runner.demo_endpoints()
        ready = [e for e in result["endpoints"] if e.get("ready")]
        checks.append({"name": "Pods behind the Service", "ok": bool(ready),
                       "detail": f"{len(ready)}/{len(result['endpoints'])} ready"})
        try:
            out = self._router_exec(cluster, f"curl -s -m 5 http://{ip}/", 15)
            ok = out["exitcode"] == 0 and "Hello" in out["stdout"]
            detail = out["stdout"].strip()[:200] if ok else (out["stderr"] or out["stdout"] or "no answer").strip()[:200]
        except (libvirt.libvirtError, TimeoutError, ValueError) as e:
            ok, detail = False, str(e)[:200]
        checks.append({"name": f"The router reaches http://{ip}/", "ok": ok, "detail": detail})
        records = (gspec.get("router") or {}).get("dns", {}).get("records") or []
        has_record = any(r.get("name") == "hello" and r.get("a") == ip for r in records)
        checks.append({"name": f"DNS hello.{gspec.get('domain')} -> {ip}", "ok": has_record,
                       "detail": "served by the router's dnsmasq" if has_record else "record missing"})
        return result


    @staticmethod
    def _bgp_checks(cluster: Cluster, group: Optional[Group], ip: str, result: Dict[str, Any]) -> None:
        """MetalLB BGP: every node has an Established session with the router, the router routes the
        service IP to the nodes (one next hop per node, ECMP)"""
        from app.services.group_service import group_service
        checks = result["checks"]
        if group is None:
            checks.append({"name": "BGP sessions with the router", "ok": False, "detail": "no lab group"})
            return
        status = group_service.bgp_status(group)
        if status.get("router_error"):
            checks.append({"name": "BGP sessions with the router", "ok": False, "detail": status["router_error"]})
            return
        sessions = {s["peer"]: s for s in status.get("sessions") or []}
        peers = [{"node": n.name, "ip": n.ip, "state": (sessions.get(n.ip) or {}).get("state", "no session")}
                 for n in cluster.nodes]
        result["bgp_peers"] = peers
        up = [p for p in peers if p["state"] == "Established"]
        checks.append({"name": "Every node has a BGP session with the router", "ok": len(up) == len(peers) and bool(peers),
                       "detail": f"{len(up)}/{len(peers)} Established"
                       + "".join(f"; {p['node']}: {p['state']}" for p in peers if p["state"] != "Established")})
        route = next((r for r in status.get("routes") or [] if r["prefix"] == f"{ip}/32"), None)
        names = {n.ip: n.name for n in cluster.nodes}
        hops = [names.get(h["ip"], h["ip"]) for h in (route or {}).get("nexthops", [])]
        result["bgp_nexthops"] = hops
        checks.append({"name": f"The router has a BGP route to {ip}", "ok": bool(route and route.get("installed")),
                       "detail": f"via {', '.join(hops)}" + (f" ({len(hops)} next hops, ECMP)" if len(hops) > 1 else "")
                       if route else "no route learned yet"})


openshift_installer = OpenShiftInstaller()
