"""Red Hat Advanced Cluster Management (ACM): hub add-on and managed cluster import (docs/openshift.md "ACM hub")

Hub = an OpenShift cluster of the app with the `acm` add-on: the advanced-cluster-management operator (its
MultiClusterHub operator subscribes to multicluster-engine itself), a MultiClusterHub with
availabilityConfig Basic (one replica per component: lab sizing) and, for non-OpenShift managed clusters, the
user's pull secret as the hub's imagePullSecret (the klusterlet comes from registry.redhat.io).

Import (POST /clusters/{hub}/acm/managed): another cluster of the app (kubeadm, k3s, OpenShift) becomes a
ManagedCluster. The klusterlet on the managed cluster connects to the hub API, api.<hub>.<domain>:6443, which is
the haproxy of the hub's router on its uplink address:
  - a cluster in another lab group: that group's router gets owned DNS records (owner "acm:<hub>")
    api.<hub zone> and *.apps.<hub zone> -> the hub router's uplink address (routers share the uplink network);
  - a k3s cluster (no router): a CoreDNS server block (configmap coredns-custom) answers the hub zone the same way.
The app checks that path first (curl from the managed side) and says in plain words when it is blocked (k3s on its
own libvirt NAT network: libvirt refuses connections between NAT networks; egress blocked in the group).
Then: Namespace + ManagedCluster + KlusterletAddonConfig on the hub, and
  - "secret": auto-import-secret with the managed cluster's kubeconfig (the hub's import controller installs the
    klusterlet), when the hub reaches the managed cluster's API (curl from the hub's router);
  - "manifests": otherwise the app applies the hub's <name>-import secret (crds.yaml + import.yaml) on the
    managed cluster itself, like `oc apply -f import.yaml` in the docs. Only managed -> hub is needed then.
Detach = delete the ManagedCluster (the hub removes the klusterlet) + the DNS entries.
"""
import base64
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

import yaml
from sqlalchemy.orm import Session

from app.libvirt_client import libvirt_client
from app.models import Cluster, Task

logger = logging.getLogger(__name__)

PACKAGE = "advanced-cluster-management"
MCE_PACKAGE = "multicluster-engine"
NAMESPACE = "open-cluster-management"
MCH_NAME = "multiclusterhub"
PULL_SECRET = "multiclusterhub-operator-pull-secret"
MCE_SUBSCRIPTION_ANNOTATION = "installer.open-cluster-management.io/mce-subscription-spec"
MCH_TIMEOUT = 40 * 60
IMPORT_TIMEOUT = 15 * 60
DETACH_TIMEOUT = 10 * 60
AGENT_NAMESPACE = "open-cluster-management-agent"

# Red Hat documents 6 vCPU / 12 GiB reserved for ACM on the hub (availabilityConfig High, 2 replicas). The app
# installs Basic (1 replica): 12 GiB and 4 vCPU above OpenShift's own minimum on the nodes that run it.
HUB_RESERVED = {"memory": 12288, "vcpu": 4}
# Node defaults when ACM is chosen at creation and no size is given (sno / master: control planes, worker)
HUB_DEFAULTS = {"sno": {"memory": 32768, "vcpu": 12}, "master": {"memory": 20480, "vcpu": 8},
                "worker": {"memory": 16384, "vcpu": 4}}
# Pod requests of ACM + MCE measured on a Basic hub (OCP 4.20.40 SNO, ACM 2.17.3: 2.8 vCPU, 8.9 GiB; OpenShift itself
# 2.7 vCPU, 12.8 GiB): what must still be schedulable before installing it on a running cluster
LIVE_REQUESTS = {"cpu_m": 2900, "memory_mi": 9216}


def owner(hub_name: str) -> str:
    return f"acm:{hub_name}"


# ------------------------------------------------------------------------------------------------- sizing

def sizing_problem(topology: str, counts: Dict[str, int], sizes: Dict[str, Dict[str, int]]) -> Optional[str]:
    """Plain words when the hub nodes can't hold ACM next to OpenShift (None = fine)"""
    from app.services.openshift_installer import MINIMUMS
    if counts.get("workers"):
        role, kind, n = "worker", "worker", counts["workers"]
    else:
        role, kind, n = "ctlplane", "sno" if topology == "sno" else "master", counts.get("masters") or 1
    size = sizes.get(role) or {}
    memory, vcpu = int(size.get("memory") or 0), int(size.get("vcpu") or 0)
    if not memory or not vcpu:
        return None
    spare_mem = n * (memory - MINIMUMS[kind]["memory"])
    spare_cpu = n * (vcpu - MINIMUMS[kind]["vcpu"])
    lacks = []
    if spare_mem < HUB_RESERVED["memory"]:
        lacks.append(f"{(HUB_RESERVED['memory'] - spare_mem + 1023) // 1024} GiB of memory")
    if spare_cpu < HUB_RESERVED["vcpu"]:
        lacks.append(f"{HUB_RESERVED['vcpu'] - spare_cpu} vCPU")
    if not lacks:
        return None
    what = {"sno": "the single node", "master": f"the {n} control planes", "worker": f"the {n} workers"}[kind]
    want = HUB_DEFAULTS[kind]
    return (f"Too small for an ACM hub: ACM needs {HUB_RESERVED['memory'] // 1024} GiB and {HUB_RESERVED['vcpu']} vCPU "
            f"on top of OpenShift's own minimum ({MINIMUMS[kind]['memory'] // 1024} GiB, {MINIMUMS[kind]['vcpu']} vCPU "
            f"per {kind if kind != 'sno' else 'single node'}). {what.capitalize()}: {memory // 1024} GiB and {vcpu} vCPU"
            f"{' each' if n > 1 else ''}, short of {' and '.join(lacks)}. Use {want['memory'] // 1024} GiB / {want['vcpu']} vCPU per node "
            "(the default when ACM is chosen at creation).")


def _cpu_m(value: Any) -> int:
    text = str(value or "0")
    if text.endswith("m"):
        return int(float(text[:-1]))
    return int(float(text) * 1000)


_UNITS = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024, "k": 1000 / 1048576, "M": 1e6 / 1048576,
          "G": 1e9 / 1048576, "T": 1e12 / 1048576}


def _mem_mi(value: Any) -> int:
    text = str(value or "0")
    m = re.match(r"^([0-9.]+)([A-Za-z]*)$", text)
    if not m:
        return 0
    number, unit = float(m.group(1)), m.group(2)
    if not unit:
        return int(number / 1048576)
    if unit == "m":  # millibytes (seen on some requests)
        return int(number / 1000 / 1048576)
    return int(number * _UNITS.get(unit, 1))


def live_headroom_problem(runner: Any) -> Optional[str]:
    """Day 2: what the schedulable nodes still have (allocatable - pod requests) vs ACM's requests"""
    try:
        nodes = runner.oc_json(["get", "nodes"], 30).get("items", [])
        pods = runner.oc_json(["get", "pods", "-A", "--field-selector=status.phase!=Succeeded,status.phase!=Failed"],
                              90).get("items", [])
    except (RuntimeError, ValueError):
        return None  # can't tell: the install itself will say
    free = {}
    for node in nodes:
        if (node.get("spec") or {}).get("unschedulable"):
            continue
        taints = (node.get("spec") or {}).get("taints") or []
        if any(t.get("effect") == "NoSchedule" for t in taints):
            continue
        alloc = (node.get("status") or {}).get("allocatable") or {}
        free[node["metadata"]["name"]] = [_cpu_m(alloc.get("cpu")), _mem_mi(alloc.get("memory"))]
    for pod in pods:
        name = (pod.get("spec") or {}).get("nodeName")
        if name not in free:
            continue
        for c in (pod.get("spec") or {}).get("containers") or []:
            req = (c.get("resources") or {}).get("requests") or {}
            free[name][0] -= _cpu_m(req.get("cpu"))
            free[name][1] -= _mem_mi(req.get("memory"))
    if not free:
        return None
    cpu = sum(max(0, v[0]) for v in free.values())
    mem = sum(max(0, v[1]) for v in free.values())
    lacks = []
    if cpu < LIVE_REQUESTS["cpu_m"]:
        lacks.append(f"{(LIVE_REQUESTS['cpu_m'] - cpu) / 1000:.1f} vCPU")
    if mem < LIVE_REQUESTS["memory_mi"]:
        lacks.append(f"{(LIVE_REQUESTS['memory_mi'] - mem + 1023) // 1024} GiB of memory")
    if not lacks:
        return None
    return (f"Not enough room left for ACM: its pods request about {LIVE_REQUESTS['cpu_m'] / 1000:.1f} vCPU and "
            f"{LIVE_REQUESTS['memory_mi'] // 1024} GiB, the schedulable nodes have {cpu / 1000:.1f} vCPU and "
            f"{mem // 1024} GiB unrequested (lacking {' and '.join(lacks)}). Give the nodes more vCPU / memory "
            "(stop the cluster, resize the node VMs, start it) or remove workloads.")


# ------------------------------------------------------------------------------------------------- hub install

def install_hub(runner: Any, pull_secret: Optional[str], mce_source: Optional[str]) -> str:
    """Operator + MultiClusterHub (Basic), waits for phase Running. Returns the ACM version."""
    ns = runner.install_operator(PACKAGE, namespace=NAMESPACE)
    manifests: List[Dict[str, Any]] = []
    spec: Dict[str, Any] = {"availabilityConfig": "Basic"}
    if pull_secret:
        runner.oc(["delete", "secret", PULL_SECRET, "-n", ns, "--ignore-not-found"], 30)
        out = runner.oc(["create", "-f", "-"], 60, stdin=json.dumps({
            "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
            "metadata": {"name": PULL_SECRET, "namespace": ns},
            "data": {".dockerconfigjson": base64.b64encode(pull_secret.encode()).decode()}}))
        if out["exitcode"] != 0:
            raise RuntimeError(f"Cannot create the hub's image pull secret: {(out['stderr'] or '').strip()[-300:]}")
        spec["imagePullSecret"] = PULL_SECRET
    mch: Dict[str, Any] = {"apiVersion": "operator.open-cluster-management.io/v1", "kind": "MultiClusterHub",
                           "metadata": {"name": MCH_NAME, "namespace": ns}, "spec": spec}
    if mce_source:
        # disconnected: the MCH operator subscribes to multicluster-engine from the mirrored catalog
        mch["metadata"]["annotations"] = {MCE_SUBSCRIPTION_ANNOTATION: json.dumps({"source": mce_source})}
    runner.log("Creating the MultiClusterHub (availability Basic)")
    runner.apply_retry(manifests + [mch], "MultiClusterHub", timeout=10 * 60)
    deadline = time.monotonic() + MCH_TIMEOUT
    last = ""
    while True:
        try:
            mch_live = runner.oc_json(["get", "multiclusterhub", MCH_NAME, "-n", ns], 30)
        except (RuntimeError, ValueError) as e:
            mch_live, last = {}, str(e)[-200:]
        status = mch_live.get("status") or {}
        phase = status.get("phase") or "Pending"
        if phase == "Running":
            return status.get("currentVersion") or ""
        components = status.get("components") or {}
        waiting = sorted(name for name, c in components.items() if c.get("status") != "True")
        message = f"MultiClusterHub {phase}"
        if components:
            message += f": {len(components) - len(waiting)}/{len(components)} components available"
            if waiting:
                message += f" (waiting: {', '.join(waiting[:4])}{'…' if len(waiting) > 4 else ''})"
        if message != last:
            runner.log(message)
            last = message
        if time.monotonic() > deadline:
            raise TimeoutError(f"ACM not running after {MCH_TIMEOUT // 60} min ({last}). "
                               "Look at `oc get mch -n open-cluster-management -o yaml` and pending pods "
                               "(`oc get pods -n open-cluster-management`, `-n multicluster-engine`).")
        runner.sleep(20)


def hub_status(runner: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        mch = runner.oc_json(["get", "multiclusterhub", MCH_NAME, "-n", NAMESPACE], 30)
        out["hub_phase"] = (mch.get("status") or {}).get("phase")
        out["hub_version"] = (mch.get("status") or {}).get("currentVersion")
    except (RuntimeError, ValueError) as e:
        out["error"] = str(e)[-300:]
        return out
    try:
        mce = runner.oc_json(["get", "multiclusterengine"], 30).get("items", [])
        if mce:
            out["mce_phase"] = (mce[0].get("status") or {}).get("phase")
    except (RuntimeError, ValueError):
        pass
    return out


def conditions_state(mc: Dict[str, Any]) -> Dict[str, Any]:
    conds = {c.get("type"): c for c in (mc.get("status") or {}).get("conditions") or []}

    def flag(name: str) -> Optional[bool]:
        c = conds.get(name)
        if c is None or c.get("status") == "Unknown":
            return None
        return c.get("status") == "True"
    labels = (mc.get("metadata") or {}).get("labels") or {}
    return {
        "joined": flag("ManagedClusterJoined"), "available": flag("ManagedClusterConditionAvailable"),
        "hub_accepted": flag("HubAcceptedManagedCluster"),
        "kubernetes_version": ((mc.get("status") or {}).get("version") or {}).get("kubernetes"),
        "vendor": labels.get("vendor"),
        "conditions": [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason"),
                        "message": (c.get("message") or "")[:300]} for c in conds.values()],
    }


# ------------------------------------------------------------------------------------------------- service

class AcmService:
    @property
    def svc(self):
        from app.services.cluster_service import cluster_service
        return cluster_service

    @property
    def installer(self):
        from app.services.openshift_installer import openshift_installer
        return openshift_installer

    def _runner(self, hub: Cluster, task: Optional[Task] = None, db: Optional[Session] = None):
        from app.services.openshift_addons import AddonRunner
        if task is None:
            return AddonRunner(hub.name, hub.version, time.sleep, lambda m: None)
        return AddonRunner(hub.name, hub.version, lambda s: self.svc._sleep(task, s),
                           lambda m: self.svc._check(task, hub, db, None, m))

    @staticmethod
    def hub_api_host(hub: Cluster) -> str:
        return f"api.{hub.name}.{hub.domain}"

    @staticmethod
    def hub_port(hub: Cluster) -> int:
        return int((hub.spec or {}).get("api_port") or 6443)

    @staticmethod
    def _entries(hub: Cluster) -> List[Dict[str, Any]]:
        return [dict(e) for e in (hub.spec or {}).get("acm_managed") or []]

    def _save_entry(self, db: Session, hub: Cluster, name: str, **values: Any) -> None:
        db.refresh(hub)
        entries = self._entries(hub)
        entry = next((e for e in entries if e["name"] == name), None)
        if entry is None:
            entry = {"name": name}
            entries.append(entry)
        entry.update(values)
        self.installer._update_spec(db, hub, acm_managed=entries)
        self.svc._publish(hub)

    def _drop_entry(self, db: Session, hub: Cluster, name: str) -> None:
        db.refresh(hub)
        self.installer._update_spec(db, hub, acm_managed=[e for e in self._entries(hub) if e["name"] != name])
        self.svc._publish(hub)

    def _addon(self, hub: Cluster) -> Dict[str, Any]:
        return next((a for a in (hub.spec or {}).get("addons") or [] if a.get("kind") == "acm"), {})

    def is_hub(self, cluster: Cluster) -> bool:
        return cluster.type == "openshift" and bool(((cluster.spec or {}).get("openshift") or {}).get("acm"))

    def relation(self, db: Session, cluster: Cluster) -> Optional[Dict[str, Any]]:
        """{role: hub | managed | spoke, hub_id, hub_name, state} for every cluster read (stored lists, no oc)"""
        from app.services.acm_spokes import provisioner
        p = provisioner(cluster)
        if p:
            return {"role": "spoke", "hub_id": p.get("hub_id"), "hub_name": p.get("hub_name"), "state": cluster.status}
        if self.is_hub(cluster):
            return {"role": "hub", "hub_id": cluster.id, "hub_name": cluster.name, "state": self._addon(cluster).get("state")}
        for hub in db.query(Cluster).filter(Cluster.type == "openshift", Cluster.id != cluster.id).all():
            entry = next((e for e in self._entries(hub) if e["name"] == cluster.name), None)
            if entry is not None:
                return {"role": "managed", "hub_id": hub.id, "hub_name": hub.name, "state": entry.get("state")}
        return None

    # ---------------------------------------------------------------------------------------- views

    def status(self, db: Session, hub: Cluster) -> Dict[str, Any]:
        enabled = self.is_hub(hub)
        addon = self._addon(hub)
        result: Dict[str, Any] = {
            "enabled": enabled, "state": addon.get("state"), "message": addon.get("message"),
            "hub_api": f"https://{self.hub_api_host(hub)}:{self.hub_port(hub)}",
        }
        if hub.type == "openshift":
            result["console_url"] = self.installer.console_url(hub) + "/multicloud/infrastructure/clusters/managed"
        ready = self.svc.to_dict(db, hub)["status"] == "ready"
        if not enabled:
            spec = hub.spec or {}
            counts = {"masters": int(spec.get("ctlplanes") or 1), "workers": int(spec.get("workers") or 0)}
            result["sizing"] = sizing_problem(((spec.get("openshift") or {}).get("topology")) or "sno", counts,
                                              {"ctlplane": spec.get("ctlplane") or {}, "worker": spec.get("worker") or {}})
            return result
        entries = {e["name"]: e for e in self._entries(hub)}
        live: Dict[str, Dict[str, Any]] = {}
        if ready and addon.get("state") == "done":
            runner = self._runner(hub)
            result.update(hub_status(runner))
            try:
                for mc in runner.oc_json(["get", "managedclusters"], 30).get("items", []):
                    live[mc["metadata"]["name"]] = conditions_state(mc)
            except (RuntimeError, ValueError) as e:
                result["error"] = str(e)[-300:]
        managed = []
        for name in list(entries) + [n for n in live if n not in entries and n != "local-cluster"]:
            entry = dict(entries.get(name) or {"name": name})
            state = live.get(name)
            if state:
                entry.update(state)
                if entry.get("state") in (None, "available", "unavailable", "unknown"):
                    entry["state"] = "available" if state["available"] else (
                        "unavailable" if state["available"] is False else "unknown")
            elif result.get("hub_phase") and entry.get("state") == "available":
                entry["state"] = "unknown"
                entry["message"] = "No ManagedCluster of that name on the hub"
            managed.append(entry)
        result["managed"] = managed
        taken = set(entries)
        importable = []
        uplink = self._uplink_network(hub)
        from app.services.acm_spokes import provisioner
        for c in db.query(Cluster).order_by(Cluster.name).all():
            if c.id == hub.id or c.name in taken or provisioner(c) is not None:
                continue
            note = None
            if c.type == "k3s" and not c.group_id and c.network != uplink:
                note = (f"on its own NAT network {c.network}: libvirt blocks connections from it to the hub's "
                        "router, the import will be refused")
            importable.append({"id": c.id, "name": c.name, "type": c.type, "status": c.status, "note": note})
        result["importable"] = importable
        from app.services.acm_spokes import spoke_service
        result["spokes"] = spoke_service.summary(db, hub)
        used = {s.get("machine") for s in result["spokes"]}
        gspec = self._group_spec(db, hub.group_id) if hub.group_id else None
        if gspec is not None:
            result["spoke_machines"] = [{"name": m.name, "memory": m.memory, "vcpu": m.vcpu}
                                        for m in gspec.members if m.bmc and m.name not in used]
        return result

    # ---------------------------------------------------------------------------------------- networking

    @staticmethod
    def _group_spec(db: Session, group_id: int):
        from app.models import Group
        from app.schemas.group import GroupSpec
        group = db.query(Group).filter(Group.id == group_id).first()
        return GroupSpec.model_validate(group.spec) if group is not None else None

    def _uplink_network(self, hub: Cluster) -> Optional[str]:
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            spec = self._group_spec(db, hub.group_id) if hub.group_id else None
            return spec.uplink if spec is not None else None
        finally:
            db.close()

    def _target_exec(self, target: Cluster) -> Callable[[str, float], Dict[str, Any]]:
        """Run a shell script where the managed cluster's traffic leaves from: its router (lab group) or its
        first control plane (k3s on a libvirt network)"""
        from app.services.group_service import router_vm_name
        if target.group_id and target.network.startswith("vmm-g-"):
            vm = router_vm_name(target.network[len("vmm-g-"):])
        else:
            node = self.svc._api_node(target)
            if node is None:
                raise ValueError(f"{target.name} has no control plane")
            vm = node.name
        return lambda script, timeout: libvirt_client.agent_exec(vm, "/bin/sh", ["-c", script], timeout=timeout)

    @staticmethod
    def _probe(run: Callable[[str, float], Dict[str, Any]], host: str, port: int) -> Optional[str]:
        """HTTP code of https://host:port/readyz ('000' = no connection / name), None if curl couldn't run"""
        url = f"https://{host}:{port}/readyz"
        try:
            out = run(f"curl -sk -o /dev/null -w '%{{http_code}}' --connect-timeout 5 --max-time 10 {url}", 30)
        except Exception as e:  # guest agent errors: libvirtError, TimeoutError
            logger.warning(f"Probe {url}: {e}")
            return None
        return (out.get("stdout") or "").strip()[-3:] or "000"

    def _check_managed_to_hub(self, db: Session, hub: Cluster, target: Cluster, hub_ip: str) -> None:
        port = self.hub_port(hub)
        if target.group_id and target.group_id != hub.group_id:
            gspec = self._group_spec(db, target.group_id)
            egress = gspec.router.egress if gspec is not None else None
            if egress is not None and egress.mode != "open":
                import ipaddress
                if not any(ipaddress.ip_address(hub_ip) in ipaddress.ip_network(a, strict=False)
                           for a in egress.allow if ":" not in a):
                    raise ValueError(
                        f"Group {gspec.name} blocks egress (mode {egress.mode}): {target.name}'s klusterlet couldn't "
                        f"reach the hub API at {hub_ip}:{port}. Add {hub_ip}/32 to the group's egress allow list "
                        "(group page, Egress) and import again.")
        code = self._probe(self._target_exec(target), hub_ip, port)
        if code is None:
            raise ValueError(f"Could not test the path from {target.name} to the hub (guest agent of its "
                             f"{'router' if target.group_id else 'control plane'} not answering): is it running?")
        if code == "000":
            if not target.group_id:
                raise ValueError(
                    f"{target.name} can't reach the hub API at {hub_ip}:{port}: the k3s cluster runs on the libvirt "
                    f"NAT network {target.network} and libvirt refuses connections from one NAT network to the "
                    f"guests of another (the hub's router is on {self._uplink_network(hub) or 'its uplink network'}). "
                    f"Create the k3s cluster on that network (network: {self._uplink_network(hub) or 'default'}), or "
                    "use a kubeadm cluster (lab group) instead.")
            raise ValueError(f"The router of {target.name}'s group can't reach the hub API at {hub_ip}:{port} "
                             "(no TCP connection): is the hub's router running?")

    def _publish_dns(self, db: Session, hub: Cluster, target: Cluster, hub_ip: str) -> None:
        """Make api.<hub zone> (and *.apps) resolve to the hub router's uplink address on the managed side"""
        zone = f"{hub.name}.{hub.domain}"
        if target.group_id:
            if target.group_id == hub.group_id:
                return
            from app.schemas.group import DNSRecord
            from app.services.group_service import group_service
            tag = owner(hub.name)

            def mutate(spec) -> None:
                names = {f"api.{zone}.", f"*.apps.{zone}."}
                spec.router.dns.records = [r for r in spec.router.dns.records
                                           if not (r.owner == tag or (r.name in names))]
                spec.router.dns.records += [DNSRecord(name=n, a=hub_ip, owner=tag) for n in sorted(names)]
            group_service.update_owned(db, target.group_id, mutate)
            return
        # k3s: CoreDNS imports /etc/coredns/custom/*.server from the optional configmap coredns-custom
        from app.services import kube_exec
        block = (f"{zone}:53 {{\n    template IN A {{\n        answer \"{{{{ .Name }}}} 60 IN A {hub_ip}\"\n    }}\n"
                 f"    template IN AAAA {{\n        rcode NOERROR\n    }}\n}}\n")
        patch = json.dumps({"data": {f"acm-{hub.name}.server": block}})
        out = kube_exec.run_kubectl(target, ["-n", "kube-system", "patch", "configmap", "coredns-custom",
                                             "--type", "merge", "-p", patch], timeout=60)
        if out["exitcode"] != 0:
            manifest = {"apiVersion": "v1", "kind": "ConfigMap",
                        "metadata": {"name": "coredns-custom", "namespace": "kube-system"},
                        "data": {f"acm-{hub.name}.server": block}}
            out = kube_exec.run_kubectl(target, ["apply", "-f", "-"], stdin=yaml.safe_dump(manifest), timeout=60)
            if out["exitcode"] != 0:
                raise RuntimeError(f"Cannot add the hub's DNS zone to {target.name}'s CoreDNS: "
                                   f"{(out['stderr'] or out['stdout']).strip()[-300:]}")
        # CoreDNS's reload plugin watches the Corefile, not the imported files
        kube_exec.run_kubectl(target, ["-n", "kube-system", "rollout", "restart", "deployment", "coredns"], timeout=60)

    def _unpublish_dns(self, db: Session, hub: Cluster, target: Optional[Cluster]) -> None:
        if target is None:
            return
        try:
            if target.group_id:
                if target.group_id == hub.group_id:
                    return
                from app.services.group_service import group_service
                tag = owner(hub.name)

                def mutate(spec) -> None:
                    spec.router.dns.records = [r for r in spec.router.dns.records if r.owner != tag]
                group_service.update_owned(db, target.group_id, mutate)
            else:
                from app.services import kube_exec
                kube_exec.run_kubectl(target, ["-n", "kube-system", "patch", "configmap", "coredns-custom", "--type",
                                               "json", "-p", json.dumps([{"op": "remove",
                                                                          "path": f"/data/acm-{hub.name}.server"}])],
                                      timeout=60)
                kube_exec.run_kubectl(target, ["-n", "kube-system", "rollout", "restart", "deployment", "coredns"],
                                      timeout=60)
        except Exception as e:  # cleanup: never block a detach / delete
            logger.warning(f"Removing the hub {hub.name}'s DNS entries from {target.name}: {e}")

    # ---------------------------------------------------------------------------------------- import

    def import_cluster(self, db: Session, hub: Cluster, cluster_id: int, method: str = "auto") -> Task:
        if hub.type != "openshift":
            raise ValueError(f"{hub.name} is a {hub.type} cluster: only an OpenShift cluster can be an ACM hub")
        if not self.is_hub(hub) or self._addon(hub).get("state") != "done":
            raise ValueError(f"ACM is not installed on {hub.name}: enable the ACM add-on first")
        if self.svc.to_dict(db, hub)["status"] != "ready":
            raise ValueError(f"Hub {hub.name} is not ready: start it first")
        target = self.svc.get_cluster(db, cluster_id)
        if target is None:
            raise LookupError("Cluster to import not found")
        if target.id == hub.id:
            raise ValueError("The hub manages itself already (local-cluster)")
        if target.name == "local-cluster":
            raise ValueError("A cluster named local-cluster can't be imported (the hub's own name for itself)")
        if any(e["name"] == target.name for e in self._entries(hub)):
            raise ValueError(f"{target.name} is already imported in {hub.name}")
        for other in db.query(Cluster).filter(Cluster.type == "openshift", Cluster.id != hub.id).all():
            if any(e["name"] == target.name for e in self._entries(other)):
                raise ValueError(f"{target.name} is already managed by the hub {other.name}: detach it there first")
        if self.svc.to_dict(db, target)["status"] != "ready":
            raise ValueError(f"{target.name} is not ready (status {target.status}): start it or wait for its install")
        from app.services.acm_spokes import provisioner
        if provisioner(target) is not None:
            raise ValueError(f"{target.name} is a spoke of ACM hub {provisioner(target).get('hub_name')}: "
                             "that hub manages it already")
        if target.type == "openshift" and self.is_hub(target):
            raise ValueError(f"{target.name} is an ACM hub itself: hubs of hubs aren't supported here")
        if not hub.api_ip:
            raise ValueError(f"Hub {hub.name} has no uplink address for its API")
        self.svc.get_kubeconfig(db, target)  # raises when none
        self.svc._ensure_idle(hub)
        # before any change: refused in plain words when the klusterlet couldn't reach the hub (a few seconds)
        self._check_managed_to_hub(db, hub, target, hub.api_ip)
        self._save_entry(db, hub, target.name, cluster_id=target.id, cluster_type=target.type, state="importing",
                         method=None, message="Queued")

        def run(db: Session, task: Task, hub_id: int, target_id: int) -> Dict[str, Any]:
            def body(hub: Cluster) -> Dict[str, Any]:
                target = self.svc.get_cluster(db, target_id)
                if target is None:
                    raise ValueError("The cluster to import was deleted")
                try:
                    used = self._import(db, task, hub, target, method)
                except Exception as e:
                    from app.services.cluster_service import Cancelled
                    self._save_entry(db, hub, target.name, state="error",
                                     message="Cancelled" if isinstance(e, Cancelled) else str(e)[-500:])
                    raise
                self.svc._set_status(db, hub, "ready", None)
                return {"imported": target.name, "method": used}
            return self.svc._guarded(db, task, hub_id, body, failed_status="ready")
        return self.svc._run_task(db, hub, "acm_import", f"Import {target.name} into", run, target.id)

    def _import(self, db: Session, task: Task, hub: Cluster, target: Cluster, method: str) -> str:
        svc = self.svc
        name = target.name
        hub_ip = hub.api_ip
        port = self.hub_port(hub)

        def step(pct: int, message: str) -> None:
            svc._check(task, hub, db, pct, f"{name}: {message}")
            self._save_entry(db, hub, name, message=message)

        step(15, f"DNS: {self.hub_api_host(hub)} -> {hub_ip} on its side")
        self._publish_dns(db, hub, target, hub_ip)
        node = self.svc._api_node(target) if target.type == "kubeadm" else None
        if target.group_id and target.group_id != hub.group_id and node is not None:
            # what the klusterlet will do: resolve the hub API name with the group router's DNS (a node uses it;
            # the router itself resolves through its uplink)
            run = lambda script, timeout: libvirt_client.agent_exec(node.name, "/bin/sh", ["-c", script],  # noqa: E731
                                                                     timeout=timeout)
            code = self._probe(run, self.hub_api_host(hub), port)
            if code == "000":
                raise ValueError(f"{self.hub_api_host(hub)} doesn't answer from {node.name} after adding its DNS "
                                 "record to the group router (router DNS not updated?)")

        runner = self._runner(hub, task, db)
        step(25, "creating the ManagedCluster and KlusterletAddonConfig on the hub")
        runner.apply_retry([
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}},
            {"apiVersion": "cluster.open-cluster-management.io/v1", "kind": "ManagedCluster",
             "metadata": {"name": name, "labels": {"cloud": "auto-detect", "vendor": "auto-detect",
                                                   "vmm-cluster-type": target.type}},
             "spec": {"hubAcceptsClient": True}},
            {"apiVersion": "agent.open-cluster-management.io/v1", "kind": "KlusterletAddonConfig",
             "metadata": {"name": name, "namespace": name},
             "spec": {"applicationManager": {"enabled": True}, "policyController": {"enabled": True},
                      "searchCollector": {"enabled": True}, "certPolicyController": {"enabled": True}}},
        ], "ManagedCluster", timeout=5 * 60)

        kubeconfig = svc.get_kubeconfig(db, target)
        used = method
        if method == "auto":
            used = "manifests"
            server = self._server(kubeconfig)
            hub_group = self._group_spec(db, hub.group_id) if hub.group_id else None
            if server and (hub_group is None or hub_group.router.egress.mode == "open"):
                from app.services.group_service import router_vm_name
                host, sport = server
                code = self._probe(lambda s, t: libvirt_client.agent_exec(router_vm_name(hub_group.name), "/bin/sh",
                                                                           ["-c", s], timeout=t), host, sport) \
                    if hub_group is not None else None
                if code and code != "000":
                    used = "secret"
        self._save_entry(db, hub, name, method=used)
        if used == "secret":
            step(35, "auto-import-secret (the hub installs the klusterlet with the cluster's kubeconfig)")
            runner.oc(["delete", "secret", "auto-import-secret", "-n", name, "--ignore-not-found"], 30)
            # oc create, not apply: no copy of the kubeconfig in a last-applied annotation
            out = runner.oc(["create", "-f", "-"], 60, stdin=json.dumps({
                "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": "auto-import-secret", "namespace": name},
                "stringData": {"autoImportRetry": "5", "kubeconfig": kubeconfig}}))
            if out["exitcode"] != 0:
                raise RuntimeError(f"auto-import-secret: {(out['stderr'] or '').strip()[-300:]}")
        else:
            step(35, "applying the hub's import manifests on the cluster (klusterlet)")
            self._apply_import(task, runner, name, target)

        step(50, "waiting for the klusterlet to join the hub")
        deadline = time.monotonic() + IMPORT_TIMEOUT
        last = ""
        while True:
            try:
                state = conditions_state(runner.oc_json(["get", "managedcluster", name], 30))
            except (RuntimeError, ValueError) as e:
                state = {"conditions": []}
                last = str(e)[-200:]
            if state.get("joined") and state.get("available"):
                self._save_entry(db, hub, name, state="available", message=None)
                return used
            pending = [c for c in state.get("conditions") or [] if c.get("status") != "True"]
            msg = "; ".join(f"{c['type']}: {c.get('message') or c.get('reason')}" for c in pending)[:300] or last
            step(60 if state.get("joined") else 50,
                 "joined, waiting for Available" if state.get("joined") else "waiting for the klusterlet to join")
            if time.monotonic() > deadline:
                raise TimeoutError(f"{name} did not become Available in {IMPORT_TIMEOUT // 60} min ({msg}). "
                                   + self._agent_diagnosis(target))
            svc._sleep(task, 15)

    @staticmethod
    def _server(kubeconfig: str) -> Optional[tuple]:
        try:
            config = yaml.safe_load(kubeconfig)
            server = config["clusters"][0]["cluster"]["server"]
        except (ValueError, KeyError, IndexError, TypeError, yaml.YAMLError):
            return None
        m = re.match(r"^https://\[?([^\]/]+?)\]?:(\d+)", server)
        return (m.group(1), int(m.group(2))) if m else None

    def _apply_import(self, task: Task, runner: Any, name: str, target: Cluster) -> None:
        from app.services import kube_exec
        deadline = time.monotonic() + 5 * 60
        while True:
            try:
                data = (runner.oc_json(["get", "secret", f"{name}-import", "-n", name], 30).get("data") or {})
            except (RuntimeError, ValueError):
                data = {}
            if data.get("crds.yaml") and data.get("import.yaml"):
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"The hub did not generate the import secret {name}-import in 5 min "
                                   "(look at the managedcluster-import-controller pods in multicluster-engine)")
            self.svc._sleep(task, 5)
        for key in ("crds.yaml", "import.yaml"):
            text = base64.b64decode(data[key]).decode()
            for attempt in range(6):
                out = kube_exec.run_kubectl(target, ["apply", "-f", "-"], stdin=text, timeout=180)
                if out["exitcode"] == 0:
                    break
                if attempt == 5:
                    raise RuntimeError(f"Applying {key} on {target.name} failed: "
                                       f"{(out['stderr'] or out['stdout']).strip()[-400:]}")
                self.svc._sleep(task, 10)  # CRDs just created: not served yet

    def _agent_diagnosis(self, target: Cluster) -> str:
        from app.services import kube_exec
        try:
            out = kube_exec.run_kubectl(target, ["get", "pods", "-n", AGENT_NAMESPACE, "--no-headers"], timeout=60)
        except Exception as e:
            return f"Could not read the klusterlet pods: {e}"
        text = (out.get("stdout") or "").strip()
        if not text:
            return f"No pod in {AGENT_NAMESPACE} on {target.name}: the klusterlet was not deployed."
        hint = ""
        if "ImagePull" in text or "ErrImage" in text:
            hint = (" Images can't be pulled: non-OpenShift clusters pull the klusterlet from registry.redhat.io with "
                    "the hub's imagePullSecret (set by the app, not on a disconnected hub).")
        return f"Klusterlet pods on {target.name}: {' | '.join(text.splitlines()[:6])}.{hint}"

    # ---------------------------------------------------------------------------------------- detach

    def detach(self, db: Session, hub: Cluster, name: str) -> Task:
        entry = next((e for e in self._entries(hub) if e["name"] == name), None)
        if name == "local-cluster":
            raise ValueError("local-cluster is the hub itself")
        if self.svc.to_dict(db, hub)["status"] != "ready":
            raise ValueError(f"Hub {hub.name} is not ready: start it first")
        if entry is not None:
            self._save_entry(db, hub, name, state="detaching", message="Queued")

        def run(db: Session, task: Task, hub_id: int, name: str) -> Dict[str, Any]:
            def body(hub: Cluster) -> Dict[str, Any]:
                runner = self._runner(hub, task, db)
                entry = next((e for e in self._entries(hub) if e["name"] == name), {})
                self.svc._check(task, hub, db, 10, f"Detaching {name} (the hub removes its klusterlet)")
                runner.oc(["delete", "managedcluster", name, "--ignore-not-found", "--wait=false"], 60)
                deadline = time.monotonic() + DETACH_TIMEOUT
                while runner.oc(["get", "managedcluster", name, "-o", "name"], 30)["exitcode"] == 0:
                    if time.monotonic() > deadline:
                        msg = (f"{name} is still being detached after {DETACH_TIMEOUT // 60} min: the hub waits for "
                               "the klusterlet's cleanup (an unreachable or deleted cluster keeps it waiting; "
                               f"`oc get managedcluster {name} -o yaml` shows the finalizers)")
                        self._save_entry(db, hub, name, state="error", message=msg)
                        raise TimeoutError(msg)
                    self.svc._sleep(task, 10)
                target = self.svc.get_cluster(db, entry.get("cluster_id")) if entry.get("cluster_id") else None
                self.svc._check(task, hub, db, 80, f"Removing the hub's DNS entries from {name}'s side")
                self._unpublish_dns(db, hub, target)
                self._drop_entry(db, hub, name)
                self.svc._set_status(db, hub, "ready", None)
                return {"detached": name}
            return self.svc._guarded(db, task, hub_id, body, failed_status="ready")
        return self.svc._run_task(db, hub, "acm_detach", f"Detach {name} from", run, name)

    # ---------------------------------------------------------------------------------------- deletes

    def forget(self, db: Session, cluster: Cluster) -> None:
        """Before a cluster is deleted: a hub drops its DNS entries on the managed side; a managed cluster is
        removed from its hub (best effort, never blocks the delete)"""
        try:
            if self.is_hub(cluster):
                for entry in self._entries(cluster):
                    target = self.svc.get_cluster(db, entry.get("cluster_id")) if entry.get("cluster_id") else None
                    self._unpublish_dns(db, cluster, target)
            for hub in db.query(Cluster).filter(Cluster.type == "openshift", Cluster.id != cluster.id).all():
                if any(e["name"] == cluster.name for e in self._entries(hub)):
                    try:
                        self._runner(hub).oc(["delete", "managedcluster", cluster.name, "--ignore-not-found",
                                              "--wait=false"], 30)
                    except (RuntimeError, ValueError) as e:
                        logger.warning(f"Deleting ManagedCluster {cluster.name} on {hub.name}: {e}")
                    self._drop_entry(db, hub, cluster.name)
        except Exception as e:
            logger.warning(f"ACM cleanup for {cluster.name}: {e}")


acm_service = AcmService()
