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
import hmac
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
from app.services import acm, budget, ipv6_service, metallb
from app.services.openshift_addons import DEMO_IMAGE, STORAGE_SERIAL, AddonRunner
from app.services import registry_service
from app.services.openshift_service import (CATALOG_INDEXES, POD_CIDR, POD_CIDR6, RESERVED_CIDRS, SERVICE_CIDR,
                                            SERVICE_CIDR6, is_okd, managed_by, managed_operator_error,
                                            okd_unsupported, openshift_service)
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

    @staticmethod
    def check_distribution(opts: OpenShiftOptions) -> None:
        """OKD: refuse in plain words what needs Red Hat-only operators / registries; OCP: a pull secret"""
        if opts.distribution != "okd":
            openshift_service.pull_secret()  # raises when missing
            return
        for kind, wanted in (("odf", opts.storage == "odf"), ("sriov", opts.sriov.enabled), ("acm", opts.acm),
                             ("disconnected", opts.disconnected)):
            if wanted:
                raise ValueError(okd_unsupported(kind))

    @staticmethod
    def check_platform(opts: OpenShiftOptions, cidr: Optional[str] = None) -> None:
        if opts.platform != "baremetal":
            return
        if opts.topology == "sno":
            raise ValueError('"platform": "baremetal" needs 3 control planes (topology compact or ha): the '
                             "installer only accepts a single node with platform none")
        if cidr:
            net = ipaddress.IPv4Network(cidr, strict=False)
            for label, vip in (("api_vip", opts.api_vip), ("ingress_vip", opts.ingress_vip)):
                if vip and ipaddress.IPv4Address(vip) not in net:
                    raise ValueError(f"{label} {vip} is not on the group network {net}")

    def create(self, db: Session, data: ClusterCreate) -> Cluster:
        opts = data.openshift or OpenShiftOptions()
        self.check_distribution(opts)
        self.check_platform(opts)
        counts = topology_counts(opts, data.workers)
        if opts.storage == "odf" and counts["masters"] + counts["workers"] < 3:
            raise ValueError('ODF needs 3 nodes: set "topology": "compact" (3 masters that also run ODF) or "ha" '
                             'with 3+ workers, or use LVMS ("storage": "lvms") on single-node OpenShift')
        enabled = {"storage:lvms": opts.storage == "lvms", "storage:odf": opts.storage == "odf",
                   "sriov": opts.sriov.enabled, "metallb": opts.metallb.enabled, "acm": opts.acm}
        for op in opts.operators:
            owner = managed_by(op.name)
            if owner and not enabled[owner]:  # it would be skipped: the option installs and configures it
                raise ValueError(managed_operator_error(op.name, day2=False))
        if opts.storage == "odf" and opts.topology == "ha" and 0 < counts["workers"] < 3:
            raise ValueError("ODF on an HA cluster runs on the workers: 3 workers at least (or use compact)")
        try:
            ipaddress.IPv4Network(opts.sriov.ipam_range, strict=False)
        except ValueError as e:
            raise ValueError(f"SR-IOV IPAM range: {e}")

        sizes = self._sizes(data, opts, counts)
        version = openshift_service.resolve_version(opts.channel, opts.version, opts.distribution)
        names = self.svc._node_names(data.name, counts["masters"], counts["workers"])

        def check(gspec) -> None:
            used = {lb.port: lb.name for lb in gspec.load_balancers}
            for port in LB_PORTS.values():
                if port in used:
                    raise ValueError(f"Lab group {gspec.name}: router port {port} is used by load balancer "
                                     f"{used[port]} (one OpenShift cluster per group, and no kubeadm cluster)")
            openshift_service.check_cidr(gspec.cidr)
            self.check_platform(opts, gspec.cidr)

        group, gspec, owned, node_subnet = self.svc._resolve_group(db, data, names, RESERVED_CIDRS, check)
        v6only = opts.ip_family == "ipv6"
        if (opts.dual_stack or v6only) and group is None:
            from app.schemas.group import IPv6Spec
            gspec.network.ipv6 = IPv6Spec(enabled=True)  # the auto-created group gets IPv6
        if v6only and not (gspec.network.ipv6 is not None and gspec.network.ipv6.enabled):
            raise ValueError(f"Lab group {gspec.name} has no IPv6: switch on IPv6 on the group first (Network & DNS "
                             "tab), or leave the group out (a new group gets IPv6)")
        ipv6_service.check_dual_stack(gspec, opts.dual_stack or v6only, {"cluster network": POD_CIDR6,
                                                                         "service network": SERVICE_CIDR6})
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
        budget.skip()  # its own stricter rule above: no generic check for the group it creates

        task = None
        if group is None:
            group, task = group_service.create_group(db, gspec)

        spec = data.model_dump()
        spec.pop("network", None)
        spec["openshift"] = {**opts.model_dump(), "version": version}
        spec.update(ctlplanes=counts["masters"], workers=counts["workers"], ctlplane=sizes["ctlplane"],
                    worker=sizes["worker"], cidr=str(node_subnet), pod_cidr=POD_CIDR, service_cidr=SERVICE_CIDR,
                    **({"pod_cidr6": POD_CIDR6, "service_cidr6": SERVICE_CIDR6}
                       if opts.dual_stack or v6only else {}),
                    api_port=LB_PORTS["api"], group_task_id=task.id if task else None, addons=[],
                    image=f"{'OKD' if is_okd(version) else 'OpenShift'} {version}")
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
        if v6only:
            topo += ", IPv6 only"
        elif opts.dual_stack:
            topo += ", dual stack"
        if opts.platform == "baremetal":
            topo += ", platform baremetal (API / ingress VIPs)"
        where = f"new lab group {group.name}" if owned else f"lab group {group.name}"
        if opts.disconnected:
            where += ", disconnected (mirror registry on the router)"
        task = task_service.start(db, TaskCreate(
            name=f"Create {'OKD' if is_okd(version) else 'OpenShift'} cluster {data.name}", type="cluster_create",
            target_type="cluster", target_id=cluster.id, target_name=data.name,
            description=f"{'OKD' if is_okd(version) else 'OpenShift'} {version}, {topo}, {where}",
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
            elif role in roles:  # sized by the request: ODF's Ceph pods need room on top of OpenShift
                extra = ODF_EXTRA[opts.odf_profile]
                need = {k: MINIMUMS[kind][k] + extra[k] for k in ("memory", "vcpu")}
                if any(res[k] < v for k, v in need.items()):
                    raise ValueError(
                        f"ODF ({opts.odf_profile} profile) runs on the {'masters' if role == 'ctlplane' else 'workers'}"
                        f": they need at least {need['memory']} MiB and {need['vcpu']} vCPU (OpenShift minimum + "
                        f"{extra['memory']} MiB / {extra['vcpu']} vCPU for Ceph), not {res['memory']} MiB / "
                        f"{res['vcpu']} vCPU. Leave \"{role}\" out to get the defaults with the ODF overhead included.")
            if not given and opts.acm and (role == "worker" or not counts["workers"]):
                # ACM hub: the nodes that run it get its documented reservation on top
                for k, v in acm.HUB_DEFAULTS[kind].items():
                    res[k] = max(res[k], v)
            if role == "ctlplane" or counts["workers"]:
                low = [f"{k} {res[k]} < {v}" for k, v in MINIMUMS[kind].items() if res[k] < v]
                if low:
                    unit = {"memory": "MiB", "vcpu": "vCPU", "disk_size": "GiB"}
                    raise ValueError(f"{'Master' if role == 'ctlplane' else 'Worker'} too small for OpenShift: "
                                     + ", ".join(f"{k} {res[k]} {unit[k]} (minimum {MINIMUMS[kind][k]})"
                                                 for k in MINIMUMS[kind] if res[k] < MINIMUMS[kind][k]))
            result[role] = res
        if opts.acm:
            problem = acm.sizing_problem(opts.topology, counts, result)
            if problem:
                raise ValueError(problem)
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
        total = budget.meminfo().get("MemTotal")  # shared reading (services/budget.py); the rule stays this one
        if total is None:
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
        if self._is_bmh(cluster):
            self._eject_bmh(db, cluster)
        else:
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
        if cluster.status != "provisioning" or not cluster.nodes:
            return False
        if self._is_bmh(cluster):
            return True  # the install carries on in the physical node by itself
        return bool((cluster.spec or {}).get("iso_path"))

    def resume(self, db: Session, task: Task, cluster_id: int) -> Dict[str, Any]:
        svc = self.svc

        def body(cluster: Cluster) -> Dict[str, Any]:
            self._set_live(cluster.id, phase="booting", assisted_status=None, progress=None, hosts=[],
                           cluster_operators=[])
            if self._is_bmh(cluster):
                # A service restart can interrupt the handoff after iDRAC has
                # accepted virtual media but before the physical node begins
                # booting. Do not merely wait for an agent in that case.
                from app.services.baremetal_service import baremetal_service
                host = baremetal_service.get_host(db, cluster.spec["bare_metal"]["host_id"])
                if host is None:
                    raise RuntimeError("The bare-metal host was deleted")
                if (baremetal_service.client(db, host).power_state() or "").lower() != "on":
                    svc._check(task, cluster, db, 40, "Restarting the bare-metal host from the agent ISO")
                    self._boot_bmh(db, cluster, (cluster.spec or {}).get("iso_path") or "")
                svc._check(task, cluster, db, 45, "Resuming after a server restart: following the install")
                return self._finish(db, task, cluster, list(cluster.nodes), (cluster.spec or {}).get("iso_path"))
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
        from app.services.openshift_day2 import ingress_ips
        ingress = ingress_ips(opts.topology, nodes)
        gspec = network.spec()
        router_ip = gspec.router.ip
        dual = bool(opts.dual_stack)  # AAAA records too (haproxy listens on the router's IPv6 address)
        # IPv6 only: the nodes' IPv4 addresses stay reserved (their host numbers give the IPv6 ones) but are
        # never leased nor in DNS; DNS is AAAA only, the router's load balancers reach the nodes over IPv6
        v6only = opts.ip_family == "ipv6"

        def backend(ip: str, port: int) -> str:
            return f"[{gspec.ip6_of(ip)}]:{port}" if v6only else f"{ip}:{port}"
        for node in nodes:
            network.reserve(node.name, node.mac, node.ip, ipv6_only=v6only)
            network.publish(node.ip, [f"{node.name}.{zone}", node.name], paired=dual, ipv6_only=v6only)
        if opts.platform == "baremetal":
            # keepalived in the cluster holds the VIPs (kept out of DHCP / static assignments like a MetalLB pool);
            # DNS points at them. The router only forwards its uplink :6443 / :80 / :443 to them: the host (and the
            # kubeconfig) reach the cluster through the router's uplink address, as with platform none.
            vips = self._assign_vips(db, cluster, network, gspec, [n.ip for n in nodes])
            network.publish(vips["api"], [f"api.{zone}", f"api-int.{zone}"], ip6=vips.get("api6"))
            network.publish(vips["ingress"], [f"*.apps.{zone}"], ip6=vips.get("ingress6"))
            network.publish_lb(f"{cluster.name}-api", LB_PORTS["api"], [f"{vips['api']}:6443"])
            network.publish_lb(f"{cluster.name}-http", LB_PORTS["http"], [f"{vips['ingress']}:80"])
            network.publish_lb(f"{cluster.name}-https", LB_PORTS["https"], [f"{vips['ingress']}:443"])
        else:
            network.publish(router_ip, [f"api.{zone}", f"api-int.{zone}", f"*.apps.{zone}"], paired=dual,
                            ipv6_only=v6only)
            network.publish_lb(f"{cluster.name}-api", LB_PORTS["api"], [backend(ip, 6443) for ip in masters])
            network.publish_lb(f"{cluster.name}-mcs", LB_PORTS["mcs"], [backend(ip, 22623) for ip in masters])
            network.publish_lb(f"{cluster.name}-http", LB_PORTS["http"], [backend(ip, 80) for ip in ingress])
            network.publish_lb(f"{cluster.name}-https", LB_PORTS["https"], [backend(ip, 443) for ip in ingress])
        if opts.metallb.enabled and not opts.metallb.pool:
            pool = self._assign_pool(network, gspec, opts.metallb.mode, opts.metallb.addresses, [n.ip for n in nodes],
                                     cluster.name, opts.metallb.bfd)
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

    def _metallb_pool6(self, db: Session, cluster: Cluster, network: GroupClusterNetwork, mode: str,
                       pool: str) -> Optional[str]:
        """Dual stack: the IPv6 side of the MetalLB pool (shared with kubeadm: metallb.dual_stack_pool): L2 = the
        IPv6 twins of the IPv4 pool, BGP = a range of the cluster's own IPv6 announce /64 (<cluster>-metallb6,
        released when going back to L2). Stored with the cluster (pool6, range6: MetalLB lab checks)."""
        if not ipv6_service.is_dual_stack(cluster):
            return None
        os_spec = cluster.spec.get("openshift") or {}
        mlb = os_spec.get("metallb") or {}
        pool6, range6 = metallb.dual_stack_pool(db, network, cluster.name, mode, pool, mlb.get("range6"))
        self._update_spec(db, cluster, openshift={**os_spec, "metallb": {**mlb, "pool6": pool6, "range6": range6}})
        return pool6

    @staticmethod
    def vips(cluster: Cluster) -> Optional[Dict[str, str]]:
        """platform baremetal: {api, ingress (+ api6, ingress6: dual stack)} once assigned, else None"""
        os_spec = (cluster.spec or {}).get("openshift") or {}
        if os_spec.get("platform") != "baremetal" or not os_spec.get("api_vip"):
            return None
        vips = {"api": os_spec["api_vip"], "ingress": os_spec["ingress_vip"]}
        if os_spec.get("api_vip6"):
            vips.update(api6=os_spec["api_vip6"], ingress6=os_spec["ingress_vip6"])
        return vips

    def _assign_vips(self, db: Session, cluster: Cluster, network: GroupClusterNetwork, gspec,
                     node_ips: List[str]) -> Dict[str, str]:
        """platform baremetal: the API / ingress VIPs (the request's, else free addresses of the group network),
        recorded in spec.openshift and reserved as one-address pools owned by the cluster. Dual stack: + their
        IPv6 twins (api_vip6 / ingress_vip6, <prefix>::<host number>): free since the IPv4 VIP is (no
        reservation pairs with it) and outside the dynamic DHCPv6 range (::dc:0-ffff)"""
        os_spec = dict(cluster.spec.get("openshift") or {})
        net = ipaddress.IPv4Network(gspec.cidr, strict=False)
        used = set(node_ips) | {gspec.router.ip}
        vips: Dict[str, str] = {}
        for kind in ("api", "ingress"):
            vip = os_spec.get(f"{kind}_vip")
            if vip:
                if ipaddress.IPv4Address(vip) not in net:
                    raise ValueError(f"{kind}_vip {vip} is not on the group network {net}")
                if vip in used:
                    raise ValueError(f"{kind}_vip {vip} is already used by a node or the router")
            else:
                vip = network.allocate_ip(list(used))
            used.add(vip)
            vips[kind] = vip
            network.set_address_pool(f"{cluster.name}-{kind}-vip", vip, vip)
        if os_spec.get("dual_stack") and gspec.ipv6_prefix():
            vips.update(api6=gspec.ip6_of(vips["api"]), ingress6=gspec.ip6_of(vips["ingress"]))
        if any(os_spec.get(f"{k}_vip{v}") != vips.get(f"{k}{v}") for k in ("api", "ingress") for v in ("", "6")):
            self._update_spec(db, cluster, openshift={**os_spec, "api_vip": vips["api"], "ingress_vip": vips["ingress"],
                                                      "api_vip6": vips.get("api6"), "ingress_vip6": vips.get("ingress6")})
        return vips

    @staticmethod
    def _assign_pool(network: GroupClusterNetwork, gspec, mode: str, size: int, node_ips: List[str],
                     cluster_name: str, bfd: bool = False) -> str:
        return metallb.assign_pool(network, gspec, mode, size, node_ips, cluster_name, bfd)

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
        if opts.acm:  # + multicluster-engine (operator_closure: RUNTIME_DEPENDENCIES)
            wanted.setdefault("redhat-operators", []).append(acm.PACKAGE)
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
        """OperatorHub source name -> mirrored CatalogSource (e.g. redhat-operators -> cs-redhat-operator-index-v4-20).
        OKD: the Red Hat operators the add-ons install come from the OKDerators catalog"""
        if is_okd(cluster.version):
            from app.services.openshift_addons import OKD_CATALOG_SOURCE
            return {"redhat-operators": OKD_CATALOG_SOURCE}
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
        # and the mirror results (written just before, by the mirror step)
        for entry in workdir.iterdir():
            if entry.name.startswith("id_ed25519") or entry.name in ("spec.json", self._mirror_file(cluster).name):
                continue
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        # the app's key + the user's (all authorized for `core`)
        ssh_key = "\n".join([openshift_service.ssh_keypair(workdir)]
                            + [k.strip() for k in spec.get("ssh_keys") or [] if k.strip()])
        network = GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))
        first = next(n for n in nodes if n.role == "ctlplane")
        v6only = opts.ip_family == "ipv6"
        gspec = network.spec()
        mirror = self.load_mirror(cluster) if opts.disconnected else None
        if opts.disconnected and mirror is None:
            raise RuntimeError("The mirror results are missing: delete the cluster and create it again")
        install = openshift_service.install_config(
            cluster.name, cluster.domain, spec["cidr"], spec["ctlplanes"], spec["workers"],
            # disconnected: the registry's credentials only (no quay.io / cloud.openshift.com: no telemetry)
            self.mirror_pull_secret(cluster) if mirror else openshift_service.pull_secret_for(cluster.version),
            ssh_key, self.install_mirror(mirror) if mirror else None,
            self.vips(cluster), gspec.ipv6_prefix() if opts.dual_stack or v6only else None,
            ipv6_only=v6only)
        hosts = [{"name": n.name, "role": "master" if n.role == "ctlplane" else "worker", "mac": n.mac} for n in nodes]
        if v6only:
            # the nodes take their IPv6 address from their DHCPv6 reservation (RA M flag; the router ignores their
            # DHCPv4 requests); rendezvous = master-0's IPv6 address, NTP = chrony on the router's IPv6 address
            agent = openshift_service.agent_config(cluster.name, gspec.ip6_of(first.ip),
                                                   gspec.ip6_of(gspec.router.ip), hosts)
        else:
            agent = openshift_service.agent_config(
                cluster.name, first.ip, network.gateway(), hosts)  # NTP: chrony on the router
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

        self._run_agent_create_image(db, task, cluster, workdir)

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

    def _run_agent_create_image(self, db: Session, task: Task, cluster: Cluster, workdir) -> None:
        """Run `openshift-install agent create image` in workdir (shared by the VM and bare-metal paths)"""
        svc = self.svc
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

    # ---------------------------------------------------------- bare metal (BMC)

    @staticmethod
    def _is_bmh(cluster: Cluster) -> bool:
        """A bare-metal OpenShift cluster: the nodes are physical hosts driven through their BMC, not
        libvirt VMs, and there is no lab group / router (the host reaches the node directly)."""
        return bool((cluster.spec or {}).get("bare_metal"))

    def create_bmh(self, db: Session, data: ClusterCreate) -> Cluster:
        """OpenShift on bare metal (docs/bmh.md). First step: single-node OpenShift on one physical host,
        standalone on its routable network (no lab group, no VPN)."""
        import secrets
        from app.services.baremetal_service import baremetal_service

        opts = data.openshift or OpenShiftOptions()
        self.check_distribution(opts)
        if opts.platform != "none":
            raise ValueError('Bare-metal hosts install with "platform": "none" (single node)')
        bm = data.bare_metal
        if opts.topology != "sno":
            raise ValueError("Bare-metal OpenShift currently supports single-node (sno) only")
        if len(bm.hosts) != 1:
            raise ValueError("Single-node OpenShift uses exactly one bare-metal host")
        if opts.disconnected:
            raise ValueError("Disconnected installs are not supported on bare metal yet")
        if opts.storage != "none" or opts.sriov.enabled or opts.metallb.enabled:
            raise ValueError("Storage / SR-IOV / MetalLB add-ons are not supported on bare metal yet")
        if db.query(Cluster).filter(Cluster.name == data.name).first():
            raise ValueError(f"Cluster '{data.name}' already exists")
        node = bm.hosts[0]
        try:
            node_ip = ipaddress.ip_address(node.ip)
            ipaddress.ip_address(bm.gateway)
            for d in bm.dns:
                ipaddress.ip_address(d)
        except ValueError as e:
            raise ValueError(f"Bare-metal networking: {e}")
        machine_net = bm.machine_network or str(ipaddress.ip_network(f"{node.ip}/{bm.prefix}", strict=False))
        openshift_service.check_cidr(machine_net)
        if node_ip not in ipaddress.ip_network(machine_net):
            raise ValueError(f"The node IP {node.ip} is not in the machine network {machine_net}")

        host = baremetal_service.get_host(db, node.host_id)
        if host is None:
            raise ValueError(f"Bare-metal host {node.host_id} not found")
        if host.cluster_name:
            raise ValueError(f"Host '{host.name}' already backs cluster '{host.cluster_name}'")
        if not host.boot_mac:
            raise ValueError(f"Host '{host.name}' has no known boot MAC: refresh its inventory or set boot_mac")

        version = openshift_service.resolve_version(opts.channel, opts.version, opts.distribution)
        counts = {"masters": 1, "workers": 0}
        sizes = self._sizes(data, opts, counts)
        spec = data.model_dump()
        spec.pop("network", None)
        spec.pop("bare_metal", None)
        spec["openshift"] = {**opts.model_dump(), "version": version}
        spec.update(ctlplanes=1, workers=0, ctlplane=sizes["ctlplane"], worker=sizes["worker"],
                    cidr=machine_net, pod_cidr=POD_CIDR, service_cidr=SERVICE_CIDR, api_port=LB_PORTS["api"],
                    addons=[], image=f"OpenShift {version}",
                    bare_metal={"host_id": host.id, "host_name": host.name, "ip": node.ip, "prefix": bm.prefix,
                                "gateway": bm.gateway, "dns": list(bm.dns), "machine_network": machine_net,
                                "mac": host.boot_mac, "root_device": bm.root_device,
                                "ntp": list(bm.ntp) or self._host_ntp_servers()})
        cluster = Cluster(
            name=data.name, type="openshift", version=version, network="bare-metal", network_owned=False,
            group_id=None, group_owned=False, domain=data.domain, spec=spec, api_ip=node.ip,
            status="provisioning", status_message="Queued",
        )
        db.add(cluster)
        db.commit()
        db.refresh(cluster)
        host.cluster_name = cluster.name
        host.status = "provisioning"
        host.status_message = None
        db.commit()
        self.save_spec(cluster)
        self.svc._publish(cluster)
        task = task_service.start(db, TaskCreate(
            name=f"Create OpenShift cluster {data.name}", type="cluster_create", target_type="cluster",
            target_id=cluster.id, target_name=data.name,
            description=f"OpenShift {version}, single node on bare-metal host {host.name}",
        ), self.provision_bmh, cluster.id)
        cluster.task_id = task.id
        db.commit()
        return cluster

    def provision_bmh(self, db: Session, task: Task, cluster_id: int) -> Dict[str, Any]:
        svc = self.svc

        def body(cluster: Cluster) -> Dict[str, Any]:
            opts = self.options(cluster)
            version = cluster.version
            self._set_live(cluster.id, phase="preparing", assisted_status=None, progress=None, hosts=[],
                           cluster_operators=[])

            def bin_progress(message: str, pct: int) -> None:
                svc._check(task, cluster, db, 5 + pct * 20 // 100, message)
            openshift_service.ensure_binaries(version, bin_progress, lambda: task_service.is_cancelled(task.id))

            svc._check(task, cluster, db, 26, "Preparing the node")
            nodes = self._allocate_bmh(db, cluster)
            # Host DNS now answers api / *.apps.<cluster>.<domain> -> the node (no router on bare metal)
            self._reconcile_dns(db)
            svc._check(task, cluster, db, 28, "Building the agent ISO (openshift-install agent create image)")
            iso = self._create_image_bmh(db, task, cluster, nodes, opts)
            svc._check(task, cluster, db, 40, "Booting the bare-metal host from the agent ISO (virtual media)")
            self._boot_bmh(db, cluster, iso)
            self._set_live(cluster.id, phase="booting")
            return self._finish(db, task, cluster, nodes, iso)

        try:
            return svc._guarded(db, task, cluster_id, body)
        except Exception:
            self._set_live(cluster_id, phase="error")
            raise

    def _allocate_bmh(self, db: Session, cluster: Cluster) -> List[ClusterNode]:
        bm = cluster.spec["bare_metal"]
        if not cluster.nodes:
            cluster.nodes.append(ClusterNode(name=f"{cluster.name}-ctlplane-0", role="ctlplane",
                                             ip=bm["ip"], mac=bm["mac"]))
        cluster.api_ip = bm["ip"]
        db.commit()
        return list(cluster.nodes)

    def _create_image_bmh(self, db: Session, task: Task, cluster: Cluster, nodes: List[ClusterNode],
                          opts: OpenShiftOptions) -> str:
        import secrets
        spec = cluster.spec
        bm = spec["bare_metal"]
        workdir = openshift_service.cluster_dir(cluster.name)
        for entry in workdir.iterdir():
            if entry.name.startswith("id_ed25519") or entry.name == "spec.json":
                continue
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        ssh_key = "\n".join([openshift_service.ssh_keypair(workdir)]
                            + [k.strip() for k in spec.get("ssh_keys") or [] if k.strip()])
        first = next(n for n in nodes if n.role == "ctlplane")
        install = openshift_service.install_config(
            cluster.name, cluster.domain, spec["cidr"], 1, 0, openshift_service.pull_secret_for(cluster.version),
            ssh_key)
        # Prepend the host resolver so the node resolves cluster names (its own, and the hub's for a spoke);
        # the user's DNS stays as a fallback. The resolver forwards everything else upstream.
        node_dns = list(bm["dns"])
        if settings.HOST_DNS_ENABLED:
            from app.services.dns_service import dns_service
            host_ip = dns_service.host_ip()
            if host_ip and host_ip not in node_dns:
                node_dns = [host_ip] + node_dns
        agent = openshift_service.agent_config_bmh(cluster.name, first.ip, [{
            "name": n.name, "role": "master" if n.role == "ctlplane" else "worker", "mac": n.mac,
            "ip": bm["ip"], "prefix": bm["prefix"], "gateway": bm["gateway"], "dns": node_dns,
            "root_device": bm.get("root_device"),
        } for n in nodes], ntp=bm.get("ntp") or None)
        for name, data in {"install-config.yaml": install, "agent-config.yaml": agent}.items():
            text = yaml.dump(data, Dumper=_BlockDumper, sort_keys=False)
            for path in (workdir / name, workdir / f"{name}.orig"):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(text)

        self._run_agent_create_image(db, task, cluster, workdir)

        kubeconfig = (workdir / "auth" / "kubeconfig").read_text()
        host_config = openshift_service.kubeconfig_for_host(
            kubeconfig, cluster.name, f"https://{first.ip}:{LB_PORTS['api']}", f"api.{self.zone(cluster)}")
        fd = os.open(workdir / "host-kubeconfig", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(host_config)
        cluster.kubeconfig = host_config
        # One-time, unguessable token for the BMC to fetch the ISO (it embeds the pull secret + ignition)
        token_file = workdir / ".iso-token"
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
        iso_file = workdir / "agent.x86_64.iso"
        if not iso_file.exists():
            raise RuntimeError("agent create image produced no agent.x86_64.iso")
        self._update_spec(db, cluster, iso_path=str(iso_file))
        return str(iso_file)

    @staticmethod
    def _host_ntp_servers() -> List[str]:
        """The host's own NTP servers (chrony.conf): a bare-metal node has no router to serve NTP, and the
        installer validates the clock. The host's NTP is reachable from the same lab network."""
        servers: List[str] = []
        for path in ("/etc/chrony.conf", "/etc/chrony/chrony.conf"):
            try:
                for line in open(path):
                    parts = line.split()
                    if parts and parts[0] in ("server", "pool") and len(parts) >= 2:
                        servers.append(parts[1])
            except OSError:
                continue
        return servers[:4]

    def _iso_token(self, cluster: Cluster) -> Optional[str]:
        try:
            return (openshift_service.cluster_dir(cluster.name) / ".iso-token").read_text().strip()
        except OSError:
            return None

    @staticmethod
    def _host_address(db: Session, cluster: Cluster) -> str:
        """host:port the BMC can reach the app on: the source IP the host uses to reach the BMC + the
        app's port (the BMC fetches the ISO itself, so it must be a routable address, not 0.0.0.0)."""
        import socket
        from urllib.parse import urlparse
        from app.services.baremetal_service import baremetal_service
        host = baremetal_service.get_host(db, cluster.spec["bare_metal"]["host_id"])
        bmc_host = urlparse(host.bmc_address).hostname if host else None
        ip = "127.0.0.1"
        if bmc_host:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((bmc_host, 443))
                ip = s.getsockname()[0]
            except OSError:
                pass
            finally:
                s.close()
        return f"{ip}:{settings.PORT}"

    def _iso_url(self, db: Session, cluster: Cluster) -> str:
        token = self._iso_token(cluster)
        # The path ends in .iso: Dell iDRAC picks the virtual-media type from the URL extension
        return f"http://{self._host_address(db, cluster)}/iso/{token}/agent.iso"

    def iso_for_token(self, db: Session, token: str):
        """The agent ISO a /iso/<token> request maps to (public download for the BMC). Only while the
        file is there (deleted after install)."""
        if not token:
            return None
        for cluster in db.query(Cluster).all():
            if not self._is_bmh(cluster):
                continue
            expected = self._iso_token(cluster)
            if expected and hmac.compare_digest(expected.encode(), token.encode()):  # constant time
                iso = openshift_service.cluster_dir(cluster.name) / "agent.x86_64.iso"
                return iso if iso.exists() else None
        return None

    def _boot_bmh(self, db: Session, cluster: Cluster, iso: str) -> None:
        from app.services.baremetal_service import baremetal_service
        host = baremetal_service.get_host(db, cluster.spec["bare_metal"]["host_id"])
        if host is None:
            raise RuntimeError("The bare-metal host was deleted")
        url = self._iso_url(db, cluster)
        bmc = baremetal_service.client(db, host)
        bmc.boot_iso_once(url)
        # Redfish accepts PowerOn before the physical machine has completed
        # POST.  Fail this task explicitly when iDRAC powers it straight back
        # off, rather than waiting for an agent that can never register.
        bmc.verify_power_on()

    def _host_curl(self, url: str, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """GET `url` from the host (bare metal: the host reaches the node directly, no router guest-exec)"""
        import urllib.error
        import urllib.request
        headers = {"User-Agent": "vm-manager"}
        if token:
            headers["Watcher-Authorization"] = token
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=8) as resp:
                return {"exitcode": 0, "stdout": resp.read().decode("utf-8", "replace")}
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def _eject_bmh(self, db: Session, cluster: Cluster) -> None:
        from app.services.baremetal_service import baremetal_service
        bm = cluster.spec.get("bare_metal") or {}
        host = baremetal_service.get_host(db, bm.get("host_id"))
        if host is not None:
            try:
                bmc = baremetal_service.client(db, host)
                bmc.eject_media()
                bmc.clear_boot_override()
            except Exception as e:  # don't fail the install over an eject
                logger.warning(f"Could not eject the agent ISO from {host.name}: {e}")
            host.status = "provisioned"
            db.commit()
        iso = (cluster.spec or {}).get("iso_path")
        if iso:
            try:
                os.unlink(iso)  # the ISO embeds the pull secret: don't leave it downloadable
            except OSError:
                pass

    def start_bmh(self, db: Session, task: Task, cluster: Cluster) -> Dict[str, Any]:
        from app.services.baremetal_service import baremetal_service
        svc = self.svc
        recovering_install = cluster.status != "ready"
        svc._set_status(db, cluster, "starting", "Powering on the bare-metal host")
        host = baremetal_service.get_host(db, cluster.spec["bare_metal"]["host_id"])
        if host is None:
            raise RuntimeError("The bare-metal host was deleted")
        # A failed initial BMH install still has its agent ISO available.  A
        # plain PowerOn would boot the disk and leave the task waiting forever
        # for an agent that was never started; reapply virtual-media boot.
        agent_iso = (cluster.spec or {}).get("iso_path")
        if recovering_install and agent_iso and os.path.isfile(agent_iso):
            svc._check(task, cluster, db, 40, "Booting the bare-metal host from the agent ISO")
            self._boot_bmh(db, cluster, agent_iso)
        else:
            baremetal_service.client(db, host).power_on()
        deadline = time.monotonic() + START_TIMEOUT
        while True:
            openshift_service.approve_csrs(cluster.name, cluster.version)
            cv = self.clusterversion(cluster)
            if cv is not None and cv.get("available"):
                return {"nodes_ready": 1}
            if time.monotonic() > deadline:
                raise TimeoutError(f"The API was not reachable {START_TIMEOUT // 60} min after power on")
            svc._check(task, cluster, db, 50, "Waiting for the node to come back")
            svc._sleep(task, 10)

    def stop_bmh(self, db: Session, task: Task, cluster: Cluster) -> Dict[str, Any]:
        from app.services.baremetal_service import baremetal_service
        svc = self.svc
        svc._set_status(db, cluster, "stopping", "Powering off the bare-metal host")
        host = baremetal_service.get_host(db, cluster.spec["bare_metal"]["host_id"])
        if host is not None:
            baremetal_service.client(db, host).power_off(graceful=True)
        svc._set_status(db, cluster, "stopped", None)
        return {"stopped": 1}

    def delete_bmh(self, db: Session, cluster: Cluster) -> None:
        """Free the host (eject media, power off, release it) and remove the install files."""
        from app.services.baremetal_service import baremetal_service
        bm = cluster.spec.get("bare_metal") or {}
        host = baremetal_service.get_host(db, bm.get("host_id"))
        if host is not None:
            try:
                bmc = baremetal_service.client(db, host)
                bmc.eject_media()
                bmc.clear_boot_override()
                bmc.power_off()
            except Exception as e:
                logger.warning(f"Could not reset the BMC of {host.name} on delete: {e}")
            host.cluster_name = None
            host.status = "available"
            host.status_message = None
            db.commit()
        shutil.rmtree(openshift_service.root / "clusters" / cluster.name, ignore_errors=True)
        with self._lock:
            self._status.pop(cluster.id, None)

    @staticmethod
    def _reconcile_dns(db: Session) -> None:
        """Refresh the host DNS resolver (groupless / bare-metal cluster api / *.apps records)."""
        try:
            from app.services.dns_service import dns_service
            dns_service.reconcile(db)
        except Exception:
            logger.warning("Host DNS reconcile failed", exc_info=True)

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

    @staticmethod
    def node_ip6(cluster: Cluster, node: ClusterNode) -> Optional[str]:
        """A node's IPv6 address (paired with its IPv4 one in the group /64)"""
        return GroupClusterNetwork(cluster.group_id, cluster.name, owned=False).spec().ip6_of(node.ip)

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
        host = first.ip
        if ipv6_service.is_ipv6_only(cluster):  # the rendezvous host only has its IPv6 address
            host = f"[{self.node_ip6(cluster, first)}]"
        url = f"http://{host}:{ASSISTED_PORT}/api/assisted-install/v2/clusters?with_hosts=true"
        if self._is_bmh(cluster):
            # bare metal: the host reaches the node directly (no router to curl from)
            out = self._host_curl(url, token)
            if out is None:
                return None
        else:
            header = f"-H {shlex.quote('Watcher-Authorization: ' + token)} " if token else ""
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
        if opts.acm:
            plan.append(("acm", acm.PACKAGE))
        managed = {"lvms-operator", "odf-operator", "local-storage-operator", "sriov-network-operator", "metallb-operator",
                   acm.PACKAGE, acm.MCE_PACKAGE}
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
        if is_okd(cluster.version):
            refused = okd_unsupported(name if kind == "storage" else kind)
            if refused:
                raise ValueError(refused)
            if kind in ("storage", "metallb", "operator"):
                runner.okd_catalog()
        if kind == "storage":
            if name == "lvms":
                runner.lvms(bare_metal=self._is_bmh(cluster))
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
            network = GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))
            metallb.record_pool(network, cluster.name, mode, pool, bool(mlb.get("bfd")))
            pool6 = self._metallb_pool6(db, cluster, network, mode, pool)
            network.commit()  # dual stack: already committed with the IPv6 range (no-op then)
            bgp = metallb.bgp_params(network.spec()) if mode == "bgp" else None
            runner.metallb(pool, mode, bgp, pool6)
            if mlb.get("service_ip"):  # day 2 (e.g. L2 -> BGP): the demo moves to the new pool
                self.run_addon(db, cluster, runner, "metallb-demo", "hello", opts)
        elif kind == "metallb-demo":
            mlb = (cluster.spec.get("openshift") or {}).get("metallb", {})
            old_ip = mlb.get("service_ip")
            dual = ipv6_service.is_dual_stack(cluster)
            ip = runner.metallb_demo(pool=mlb.get("pool"), mode=mlb.get("mode") or "l2", dual_stack=dual)
            ip6 = runner.service_ip6() if dual else None
            network = GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))
            gspec = network.spec()
            hostname = mlb.get("hostname") or metallb.demo_hostname(gspec, cluster.name)
            if old_ip and old_ip != ip:
                network.unpublish(old_ip)
            network.publish(ip, [f"{hostname}.{gspec.domain}"], ip6=ip6)
            network.commit()
            os_spec = cluster.spec.get("openshift") or {}
            self._update_spec(db, cluster, openshift={**os_spec, "metallb": {**os_spec.get("metallb", {}),
                                                                               "demo": True, "service_ip": ip,
                                                                               "service_ip6": ip6,
                                                                               "hostname": hostname}})
        elif kind == "acm":
            # non-OpenShift managed clusters pull the klusterlet from registry.redhat.io with the hub's pull
            # secret (MultiClusterHub imagePullSecret); a disconnected hub has only its mirror's credentials
            runner.acm(None if opts.disconnected else openshift_service.pull_secret(),
                       runner.sources.get("redhat-operators"))
        elif kind == "gitops":
            from app.services.gitops_service import gitops_service
            runner.install_operator("openshift-gitops-operator", namespace="openshift-gitops-operator")
            gitops_service.finish_argocd_install(runner)
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
        refused = request.operator and managed_operator_error(request.operator.name, day2=True)
        if request.kind == "operator" and refused:  # a bare operator without the app's setup (disks, CRs, pools)
            raise ValueError(refused)
        if is_okd(cluster.version) and okd_unsupported(request.kind):
            raise ValueError(okd_unsupported(request.kind))
        if request.kind in ("metallb", "metallb-demo") and ipv6_service.is_ipv6_only(cluster):
            raise ValueError("MetalLB on IPv6-only OpenShift is not done yet (its pools are IPv4)")
        if request.kind == "odf" and len(cluster.nodes) < 3:
            raise ValueError("ODF needs 3 nodes (Ceph replicates over 3): use LVMS on this cluster")
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
            os_spec["storage"] = kind
            # Bare-metal nodes use their own spare disks (LVMS auto-discovers them); only VM nodes need the
            # app-attached vmm-storage virtio disk: hot-plugged here when missing (as at create).
            missing = [] if self._is_bmh(cluster) else [
                n.name for n in cluster.nodes
                if n.role in storage_roles(OpenShiftOptions.model_validate(os_spec), workers)
                and not self._has_storage_disk(n.name)]
            # already attached "for the next start" by an earlier try: don't attach a second disk
            pending = [vm for vm in missing if self._has_storage_disk(vm, inactive=True)]
            pending += [vm for vm in missing if vm not in pending
                        and self._attach_storage_disk(vm, int(os_spec.get("storage_disk_size") or 100))["pending"]]
            if pending:
                raise ValueError(f"The storage disk (serial '{STORAGE_SERIAL}') could not be hot-plugged into "
                                 f"{', '.join(pending)}: it is attached for the next start. Stop and start the "
                                 f"cluster, then add {kind} again")
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
                                                [n.ip for n in cluster.nodes], cluster.name, bool(mlb.get("bfd")))
                # the new pool (and the other mode's entry dropped) in the group spec + router: BGP announce
                # range accepted by FRR, L2 range kept out of DHCP
                network.commit()
            elif mode == "bgp" and mlb.get("bfd"):  # BFD asked on an existing BGP pool: turn it on on the router
                network = GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))
                network.enable_bfd()
                network.commit()
            os_spec["metallb"] = mlb
            name = "metallb-operator"
        elif kind == "metallb-demo":
            if not (os_spec.get("metallb") or {}).get("pool"):
                raise ValueError("Enable MetalLB first")
            name = "hello"
        elif kind == "acm":
            spec = cluster.spec or {}
            counts = {"masters": int(spec.get("ctlplanes") or 1), "workers": int(spec.get("workers") or 0)}
            # Bare-metal nodes: the spec sizes are nominal (the physical host is much bigger), so the static
            # size check doesn't apply — the live headroom check reads the node's real allocatable capacity.
            problem = None if spec.get("bare_metal") else acm.sizing_problem(
                os_spec.get("topology") or "sno", counts,
                {"ctlplane": spec.get("ctlplane") or {}, "worker": spec.get("worker") or {}})
            problem = problem or acm.live_headroom_problem(AddonRunner(cluster.name, cluster.version, time.sleep,
                                                                       lambda m: None))
            if problem:
                raise ValueError(problem)
            os_spec["acm"] = True
            name = acm.PACKAGE
        elif kind == "gitops":
            # OpenShift GitOps (Argo CD): no sizing gate (a small operator); it closes the git -> deploy loop
            # for ACM spokes (the hub's Argo CD syncs the spoke manifests from the in-lab git server).
            if not os_spec.get("acm"):
                raise ValueError("Enable ACM first: GitOps deploys the hub's spokes")
            os_spec["gitops"] = True
            name = "openshift-gitops-operator"
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
                    runner = self._runner(db, task, cluster)
                    self.run_addon(db, cluster, runner, kind, name, opts)
                except Exception as e:
                    self._set_addon(db, cluster, kind, name, "error", str(e)[-500:])
                    raise
                self._set_addon(db, cluster, kind, name, "done")
                # first day-2 enable with demo (the default): like at install, the demo follows the operator
                # (a mode switch already moved an existing demo in run_addon)
                if kind == "metallb" and opts.metallb.demo \
                        and not (cluster.spec.get("openshift") or {}).get("metallb", {}).get("service_ip"):
                    self._set_addon(db, cluster, "metallb-demo", "hello", "installing")
                    try:
                        self.run_addon(db, cluster, runner, "metallb-demo", "hello", opts)
                    except Exception as e:
                        self._set_addon(db, cluster, "metallb-demo", "hello", "error", str(e)[-500:])
                        raise
                    self._set_addon(db, cluster, "metallb-demo", "hello", "done")
                self.svc._set_status(db, cluster, "ready", None)
                return {"addon": name}
            return self.svc._guarded(db, task, cluster_id, body, failed_status="ready")
        self._set_addon(db, cluster, kind, name, "pending")
        return self.svc._run_task(db, cluster, "addon", f"Add {name} to", run)

    @staticmethod
    def _has_storage_disk(vm: str, inactive: bool = False) -> bool:
        xml = libvirt_client.get_vm_xml(vm, inactive=inactive) or ""
        return f"<serial>{STORAGE_SERIAL}</serial>" in xml

    @staticmethod
    def _attach_storage_disk(vm: str, size_gb: int) -> Dict[str, Any]:
        """Day-2 LVMS / ODF: the empty virtio disk with serial vmm-storage (hot-plugged when the node runs)"""
        pool_name = libvirt_client.ensure_pool(settings.DEFAULT_POOL_NAME, settings.DEFAULT_POOL_PATH).name()
        path = libvirt_client.create_volume(pool_name, libvirt_client.free_volume_name(pool_name, f"{vm}-storage", "qcow2"),
                                            size_gb * 1024 ** 3, "qcow2")
        try:
            return libvirt_client.attach_disk(vm, domain_xml.data_disk_xml(
                path, libvirt_client.next_disk_target(vm, "vd"), "virtio", "qcow2", serial=STORAGE_SERIAL))
        except Exception:
            libvirt_client.delete_volume_by_path(path)
            raise

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
        # A day-2 add-on (operator / storage / ...) on an already-installed cluster: not the initial install,
        # so the UI shows the add-on's own progress, not the cluster's install timeline.
        task = db.query(Task).filter(Task.id == cluster.task_id).first() if cluster.task_id else None
        day2 = bool(task_service.is_running(cluster.task_id) and task is not None
                    and task.type not in ("cluster_create", "cluster_resume"))
        # adding / removing workers, upgrades: their own cards (nodes, upgrades) show them; the cluster is installed
        if task is not None and task.type in ("cluster_scale", "cluster_upgrade") and cluster.kubeconfig \
                and status in ("provisioning", "ready"):
            ops = self.cluster_operators(cluster)
            cv = self.clusterversion(cluster) if ops is not None else None
            return {"phase": "ready", "version": (cv or {}).get("version") or cluster.version,
                    "cluster_operators": ops or [], "addons": addons, "hosts": []}
        if status == "provisioning" or task_service.is_running(cluster.task_id):
            return {"phase": "addons" if day2 else (live.get("phase") or "preparing"), "day2": day2,
                    "assisted_status": live.get("assisted_status"),
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
        """The MetalLB lab tab: shared with kubeadm (metallb.scenario), through oc on the host"""
        mlb = ((cluster.spec or {}).get("openshift") or {}).get("metallb") or {}
        group = db.query(Group).filter(Group.id == cluster.group_id).first()
        ready = self.svc.to_dict(db, cluster)["status"] == "ready"
        runner = AddonRunner(cluster.name, cluster.version, time.sleep, lambda m: None)
        return metallb.scenario(cluster, group, mlb, runner, lambda script, t: self._router_exec(cluster, script, t), ready)


openshift_installer = OpenShiftInstaller()
