"""MetalLB on kubeadm clusters, installed and configured by the app (docs/bgp.md)

Upstream MetalLB (METALLB_VERSION, FRR mode: BFD needs FRR) applied with kubectl on a control plane
through the guest agent, like the rest of the kubeadm orchestration; the configuration, demo, pools and
live checks are shared with OpenShift's add-on (services/metallb.py). The state lives in
cluster.spec["kubeadm"]["metallb"]: the options (enabled, mode, addresses, demo, bfd) + pool, service_ip,
hostname and the last apply's state / message.
"""
import logging
from typing import Any, Dict, List, Optional

import libvirt
from sqlalchemy.orm import Session

from app.libvirt_client import libvirt_client
from app.models import Cluster, Group, Task
from app.schemas.openshift import MetalLBOptions
from app.services import metallb
from app.services.cluster_drivers import ADMIN_CONF
from app.services.cluster_network import GroupClusterNetwork

logger = logging.getLogger(__name__)

METALLB_VERSION = "v0.16.1"
METALLB_MANIFEST = f"https://raw.githubusercontent.com/metallb/metallb/{METALLB_VERSION}/config/manifests/metallb-frr.yaml"
MANIFEST_PATH = f"/root/metallb-frr-{METALLB_VERSION}.yaml"
ROLLOUT_TIMEOUT = 10 * 60  # image pulls (controller, speaker, FRR)


class NodeKubectl(metallb.MetalLBClient):
    """kubectl on a control plane (admin.conf), through the guest agent"""

    def __init__(self, vm: str, sleep=None, log=None):
        self.vm = vm
        if sleep is not None:
            self.sleep = sleep
        if log is not None:
            self.log = log

    def kube(self, args: List[str], timeout: float = 60, stdin: Optional[str] = None) -> Dict[str, Any]:
        try:
            return libvirt_client.agent_exec(self.vm, "/usr/bin/kubectl", ["--kubeconfig", ADMIN_CONF] + args,
                                             input_data=stdin.encode() if stdin is not None else None, timeout=timeout)
        except (libvirt.libvirtError, TimeoutError) as e:
            return {"exitcode": 125, "stdout": "", "stderr": f"{self.vm}: {e}"}

    def sh(self, script: str, timeout: float, label: str) -> str:
        try:
            out = libvirt_client.agent_exec(self.vm, "/bin/sh", ["-c", script], timeout=timeout)
        except (libvirt.libvirtError, TimeoutError) as e:
            raise RuntimeError(f"{label} on {self.vm}: {e}")
        if out["exitcode"] != 0:
            raise RuntimeError(f"{label} failed on {self.vm}: {(out['stdout'] + out['stderr']).strip()[-1500:]}")
        return out["stdout"]

    def install(self) -> None:
        """Upstream manifest (downloaded by the node, ~100 KB of CRDs: too big for guest-exec input), then
        wait for the controller (it serves the webhook the configuration goes through) and the speakers"""
        self.log(f"Installing MetalLB {METALLB_VERSION} (FRR mode)")
        self.sh(f"test -s {MANIFEST_PATH} || {{ n=0; until curl -fsSL -o {MANIFEST_PATH}.tmp {METALLB_MANIFEST}; do "
                "n=$((n+1)); [ $n -ge 20 ] && exit 1; sleep 10; done; mv " + f"{MANIFEST_PATH}.tmp {MANIFEST_PATH}; }}; "
                f"kubectl --kubeconfig {ADMIN_CONF} apply -f {MANIFEST_PATH}", 300, "MetalLB manifest")
        self.log("Waiting for the MetalLB controller and speakers")
        for what in ("deployment/controller", "daemonset/speaker"):
            out = self.kube(["-n", metallb.NAMESPACE, "rollout", "status", what, f"--timeout={ROLLOUT_TIMEOUT}s"],
                            ROLLOUT_TIMEOUT + 30)
            if out["exitcode"] != 0:
                raise RuntimeError(f"MetalLB {what} not ready: {(out['stderr'] or out['stdout']).strip()[-500:]}")

    def uninstall(self) -> None:
        self.remove_demo()
        self.sh(f"test -s {MANIFEST_PATH} || curl -fsSL -o {MANIFEST_PATH} {METALLB_MANIFEST}; "
                f"kubectl --kubeconfig {ADMIN_CONF} delete -f {MANIFEST_PATH} --ignore-not-found --wait=false",
                300, "MetalLB removal")


def _svc():
    from app.services.cluster_service import cluster_service
    return cluster_service


def options(cluster: Cluster) -> Dict[str, Any]:
    return dict(((cluster.spec or {}).get("kubeadm") or {}).get("metallb") or {})


def _save(db: Session, cluster: Cluster, **values: Any) -> Dict[str, Any]:
    spec = dict(cluster.spec or {})
    kubeadm = dict(spec.get("kubeadm") or {})
    kubeadm["metallb"] = {**(kubeadm.get("metallb") or {}), **values}
    spec["kubeadm"] = kubeadm
    cluster.spec = spec
    db.commit()
    return kubeadm["metallb"]


def _network(cluster: Cluster) -> GroupClusterNetwork:
    return GroupClusterNetwork(cluster.group_id, cluster.name, owned=bool(cluster.group_owned))


def _client(cluster: Cluster, task: Optional[Task] = None, db: Optional[Session] = None) -> NodeKubectl:
    svc = _svc()
    node = svc._api_node(cluster)
    if node is None:
        raise ValueError("The cluster has no control plane")
    if task is None:
        return NodeKubectl(node.name)
    return NodeKubectl(node.name, sleep=lambda s: svc._sleep(task, s),
                       log=lambda m: svc._check(task, cluster, db, None, m))


def apply(db: Session, task: Task, cluster: Cluster) -> Dict[str, Any]:
    """Install / configure MetalLB as the stored options say (task body: create and day 2).
    Assigns the pool (other mode's pool dropped), applies the configuration, (re)deploys the demo and
    publishes its DNS name. Raises on failure (state = error)."""
    mlb = options(cluster)
    mode = mlb.get("mode") or "l2"
    _save(db, cluster, state="installing", message=None)
    try:
        network = _network(cluster)
        gspec = network.spec()
        if not mlb.get("pool"):
            mlb["pool"] = metallb.assign_pool(network, gspec, mode, int(mlb.get("addresses") or 16),
                                              [n.ip for n in cluster.nodes], cluster.name, bool(mlb.get("bfd")))
        else:  # (re)record the pool in the group spec: idempotent, repairs a spec edited meanwhile
            name = f"{cluster.name}-metallb"
            if mode == "bgp":
                network.set_bgp_range(name, mlb["pool"], bfd=bool(mlb.get("bfd")))
            else:
                start, end = mlb["pool"].split("-")
                network.set_address_pool(name, start, end)
        network.commit()
        _save(db, cluster, pool=mlb["pool"])
        gspec = network.spec()
        client = _client(cluster, task, db)
        client.install()
        client.configure(mlb["pool"], mode, metallb.bgp_params(gspec) if mode == "bgp" else None)
        if mlb.get("demo", True):
            ip = client.deploy_demo(mlb["pool"], mode)
            hostname = mlb.get("hostname") or metallb.demo_hostname(gspec, cluster.name)
            old = mlb.get("service_ip")
            if old and old != ip:
                network.unpublish(old)
            network.publish(ip, [f"{hostname}.{gspec.domain}"])
            network.commit()
            _save(db, cluster, service_ip=ip, hostname=hostname)
        elif mlb.get("service_ip"):
            client.remove_demo()
            network.unpublish(mlb["service_ip"])
            network.commit()
            _save(db, cluster, service_ip=None)
    except Exception as e:
        db.rollback()
        _save(db, cluster, state="error", message=str(e)[-500:])
        raise
    final = _save(db, cluster, state="done", message=None)
    return {"metallb": {k: final.get(k) for k in ("mode", "pool", "service_ip", "hostname")}}


def remove(db: Session, task: Task, cluster: Cluster) -> Dict[str, Any]:
    """MetalLB disabled: demo, configuration and MetalLB deleted, pool and DNS name released"""
    mlb = options(cluster)
    try:
        _client(cluster, task, db).uninstall()
    except (RuntimeError, ValueError) as e:  # cluster unreachable: still release the group's entries
        logger.warning(f"MetalLB removal on {cluster.name}: {e}")
    network = _network(cluster)
    network.remove_address_pool(f"{cluster.name}-metallb")
    network.remove_bgp_range(f"{cluster.name}-metallb")
    if mlb.get("service_ip"):
        network.unpublish(mlb["service_ip"])
    network.commit()
    _save(db, cluster, enabled=False, pool=None, service_ip=None, state=None, message=None)
    return {"metallb": "removed"}


def check_create(data: Any) -> None:
    """ClusterCreate validation of the kubeadm options"""
    if data.kubeadm is not None and data.type != "kubeadm":
        raise ValueError("'kubeadm' options are for type kubeadm")


def initial(db: Session, task: Task, cluster: Cluster) -> Optional[str]:
    """At the end of a create: MetalLB when asked. A failure doesn't fail the cluster (returned message)."""
    if not options(cluster).get("enabled"):
        return None
    _svc()._check(task, cluster, db, 96, "Installing MetalLB")
    try:
        apply(db, task, cluster)
        return None
    except Exception as e:
        from app.services.cluster_service import Cancelled
        if isinstance(e, Cancelled):
            raise
        logger.exception(f"MetalLB on {cluster.name}")
        return f"MetalLB failed: {str(e)[-300:]}"


def set_options(db: Session, cluster: Cluster, body: MetalLBOptions) -> Task:
    """PUT /clusters/{id}/metallb on a kubeadm cluster: enable / switch mode / demo / BFD / disable"""
    svc = _svc()
    if cluster.type != "kubeadm":
        raise ValueError("Only kubeadm clusters (OpenShift: the MetalLB add-on)")
    if not cluster.group_id:
        raise ValueError("The cluster is not in a lab group")
    if svc.to_dict(db, cluster)["status"] != "ready":
        raise ValueError("Start the cluster first")
    svc._ensure_idle(cluster)
    prev = options(cluster)
    if not body.enabled:
        if not prev.get("enabled"):
            raise ValueError("MetalLB is not enabled")
        func, label = remove, "Remove MetalLB from"
    else:
        new = {**prev, **body.model_dump(exclude={"pool"}), "enabled": True}
        if (prev.get("mode") or "l2") != body.mode or not prev.get("enabled"):
            # a pool of the other kind (or none yet): apply() assigns it, dropping the old one from the group
            new["pool"] = None
        _save(db, cluster, **{**new, "state": "pending", "message": None})
        func, label = apply, "Configure MetalLB on"

    def run(db: Session, task: Task, cluster_id: int) -> Dict[str, Any]:
        def body_(cluster: Cluster) -> Dict[str, Any]:
            result = func(db, task, cluster)
            svc._set_status(db, cluster, "ready", None)
            return result
        return svc._guarded(db, task, cluster_id, body_, failed_status="ready")
    return svc._run_task(db, cluster, "metallb", label, run)


def scenario(db: Session, cluster: Cluster) -> Dict[str, Any]:
    from app.services.group_service import router_vm_name
    group = db.query(Group).filter(Group.id == cluster.group_id).first() if cluster.group_id else None
    ready = _svc().to_dict(db, cluster)["status"] == "ready"
    client = None
    if ready:
        try:
            client = _client(cluster)
        except ValueError:
            client = None

    def router_exec(script: str, timeout: float) -> Dict[str, Any]:
        if group is None:
            raise ValueError("no lab group")
        return libvirt_client.agent_exec(router_vm_name(group.name), "/bin/sh", ["-c", script], timeout=timeout)
    return metallb.scenario(cluster, group, options(cluster), client, router_exec, ready)


def announcing_node(cluster: Cluster) -> Optional[str]:
    """Topology view: the node answering ARP for the demo IP (L2)"""
    try:
        return _client(cluster).announcing_node()
    except ValueError:
        return None
