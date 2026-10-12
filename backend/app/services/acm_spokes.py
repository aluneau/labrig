"""ACM hub provisions a single-node OpenShift "spoke" on emulated bare metal (docs/openshift.md "Provision a spoke").

The machine is a BMC member of the hub's lab group (bmc_service: Redfish emulator on the host), so it resolves and
reaches the hub's routes through the group router like the hub itself. On the hub (central infrastructure
management, the customer's ZTP building blocks):
  Provisioning (metal3 on a non-bare-metal hub: provisioningNetwork Disabled, watchAllNamespaces) +
  AgentServiceConfig (assisted service, volumes on the hub's default StorageClass), then per spoke:
  Namespace, pull secret, BMC secret, ClusterImageSet, ClusterDeployment + AgentClusterInstall (SNO, user-managed
  networking), InfraEnv, BareMetalHost (redfish-virtualmedia, bootMode legacy, inspection off: the assisted
  agent reports the hardware), ManagedCluster + KlusterletAddonConfig (imported once installed).
metal3 inserts the InfraEnv's discovery ISO through the BMC (virtual media) and powers the machine on; the agent
registers, the BareMetalHost agent controller approves it and the assisted service installs. The group router serves
api / api-int / *.apps.<spoke>.<domain> -> the machine (owned records "acm-spoke:<name>").
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import Cluster, Group, Task

logger = logging.getLogger(__name__)

MACHINE_NS = "openshift-machine-api"
MCE_NS = "multicluster-engine"
CIM_TIMEOUT = 25 * 60
DISCOVERY_TIMEOUT = 40 * 60
INSTALL_TIMEOUT = 150 * 60


def owner(name: str) -> str:
    return f"acm-spoke:{name}"


class SpokeService:
    @property
    def acm(self):
        from app.services.acm import acm_service
        return acm_service

    @property
    def svc(self):
        from app.services.cluster_service import cluster_service
        return cluster_service

    @staticmethod
    def _entries(hub: Cluster) -> List[Dict[str, Any]]:
        return [dict(e) for e in (hub.spec or {}).get("acm_spokes") or []]

    def _save(self, db: Session, hub: Cluster, name: str, **values: Any) -> None:
        db.refresh(hub)
        entries = self._entries(hub)
        entry = next((e for e in entries if e["name"] == name), None)
        if entry is None:
            entry = {"name": name}
            entries.append(entry)
        entry.update(values)
        self.acm.installer._update_spec(db, hub, acm_spokes=entries)
        self.svc._publish(hub)

    def _drop(self, db: Session, hub: Cluster, name: str) -> None:
        db.refresh(hub)
        self.acm.installer._update_spec(db, hub, acm_spokes=[e for e in self._entries(hub) if e["name"] != name])
        self.svc._publish(hub)

    # ------------------------------------------------------------------------------------------------ hub side

    def ensure_cim(self, runner: Any) -> None:
        """metal3 + assisted service on the hub (idempotent)"""
        runner.log("Hub: metal3 (Provisioning, no provisioning network) for BareMetalHosts")
        runner.apply_retry([{"apiVersion": "metal3.io/v1alpha1", "kind": "Provisioning",
                             "metadata": {"name": "provisioning-configuration"},
                             "spec": {"provisioningNetwork": "Disabled", "watchAllNamespaces": True}}],
                           "Provisioning", timeout=5 * 60)
        scs = runner.oc_json(["get", "storageclass"], 30).get("items", [])
        if not any((sc.get("metadata", {}).get("annotations") or {}).get("storageclass.kubernetes.io/is-default-class")
                   == "true" for sc in scs):
            raise ValueError("The hub has no default StorageClass: the assisted service needs volumes. Add LVM Storage "
                             "(Operators tab) first.")
        runner.log("Hub: assisted service (AgentServiceConfig)")

        def volume(size: str) -> Dict[str, Any]:
            return {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}}
        runner.apply_retry([{"apiVersion": "agent-install.openshift.io/v1beta1", "kind": "AgentServiceConfig",
                             "metadata": {"name": "agent"},
                             "spec": {"databaseStorage": volume("10Gi"), "filesystemStorage": volume("20Gi"),
                                      "imageStorage": volume("10Gi")}}], "AgentServiceConfig", timeout=5 * 60)
        deadline = time.monotonic() + CIM_TIMEOUT
        last = ""
        while True:
            waiting = []
            for ns, name in ((MACHINE_NS, "metal3"), (MCE_NS, "assisted-service"), (MCE_NS, "assisted-image-service")):
                kind = "statefulset" if name == "assisted-image-service" else "deployment"
                out = runner.oc(["get", kind, name, "-n", ns, "-o", "jsonpath={.status.readyReplicas}"], 30)
                if out["exitcode"] != 0 or not (out["stdout"].strip() or "0").isdigit() or int(out["stdout"].strip() or 0) < 1:
                    waiting.append(name)
            if not waiting:
                return
            co = runner.oc(["get", "co", "baremetal", "-o",
                            "jsonpath={.status.conditions[?(@.type=='Disabled')].status} "
                            "{.status.conditions[?(@.type=='Available')].message}"], 30)["stdout"].strip()
            message = f"waiting for {', '.join(waiting)}" + (f" (baremetal operator: {co})" if co else "")
            if message != last:
                runner.log(f"Hub: {message}")
                last = message
            if time.monotonic() > deadline:
                raise TimeoutError(f"Central infrastructure management not ready after {CIM_TIMEOUT // 60} min: "
                                   f"{message}")
            runner.sleep(15)

    # ------------------------------------------------------------------------------------------------ spoke

    def provision(self, db: Session, hub: Cluster, name: str, machine: str, version: Optional[str]) -> Task:
        from app.services import bmc_service
        from app.services.openshift_service import openshift_service
        if not self.acm.is_hub(hub) or self.acm._addon(hub).get("state") != "done":
            raise ValueError(f"ACM is not installed on {hub.name}: enable it first")
        if self.svc.to_dict(db, hub)["status"] != "ready":
            raise ValueError(f"Hub {hub.name} is not ready")
        if any(e["name"] == name for e in self._entries(hub)) \
                or db.query(Cluster).filter(Cluster.name == name).first() is not None:
            raise ValueError(f"A cluster named {name} already exists")
        group = db.query(Group).filter(Group.id == hub.group_id).first()
        spec = group.spec or {}
        member = next((m for m in spec.get("members") or [] if m["name"] == machine), None)
        if member is None or not member.get("bmc"):
            raise ValueError(f"{machine} is not a BMC member of the hub's group {group.name}: add spoke machines "
                             "first (group page, Spoke machines for the hub)")
        if int(member.get("memory") or 0) < 16384 or int(member.get("vcpu") or 0) < 8:
            raise ValueError(f"{machine} is too small for single-node OpenShift (16 GiB / 8 vCPU minimum)")
        if any(e.get("machine") == machine for e in self._entries(hub)):
            raise ValueError(f"{machine} is already used by another spoke")
        status = bmc_service.status(group)
        if not status["running"] or bmc_service.available():
            raise ValueError(f"The group's BMC emulator is not running: {status.get('error') or 'see the group page'}")
        host = next(h for h in status["members"] if h["name"] == machine)
        if not host["redfish_url"]:
            raise ValueError(f"{machine}: its VM is missing")
        version = version or hub.version
        openshift_service.pull_secret()
        self.svc._ensure_idle(hub)
        self._save(db, hub, name, machine=machine, group_id=group.id, version=version, state="pending",
                   message="Queued", ip=member.get("ip"))

        def run(db: Session, task: Task, hub_id: int) -> Dict[str, Any]:
            def body(hub: Cluster) -> Dict[str, Any]:
                try:
                    self._provision(db, task, hub, name, group, member, host, version)
                except Exception as e:
                    from app.services.cluster_service import Cancelled
                    self._save(db, hub, name, state="error",
                               message="Cancelled" if isinstance(e, Cancelled) else str(e)[-500:])
                    raise
                self.svc._set_status(db, hub, "ready", None)
                return {"spoke": name}
            return self.svc._guarded(db, task, hub_id, body, failed_status="ready")
        return self.svc._run_task(db, hub, "acm_spoke", f"Provision spoke {name} from", run)

    def _provision(self, db: Session, task: Task, hub: Cluster, name: str, group: Group, member: Dict[str, Any],
                   host: Dict[str, Any], version: str) -> None:
        from app.services import bmc_service
        from app.services.group_service import group_service
        from app.schemas.group import DNSRecord
        from app.services.openshift_service import openshift_service
        runner = self.acm._runner(hub, task, db)
        svc = self.svc

        def step(pct: int, message: str, state: str = "provisioning") -> None:
            svc._check(task, hub, db, pct, f"Spoke {name}: {message}")
            self._save(db, hub, name, state=state, message=message)

        step(5, "central infrastructure management on the hub (metal3, assisted service)")
        self.ensure_cim(runner)

        domain = (group.spec or {}).get("domain") or f"{group.name}.lab"
        zone = f"{name}.{domain}"
        ip = member["ip"]
        tag = owner(name)

        def records(spec) -> None:
            names = {f"api.{zone}.", f"api-int.{zone}.", f"*.apps.{zone}."}
            spec.router.dns.records = [r for r in spec.router.dns.records if r.owner != tag and r.name not in names]
            spec.router.dns.records += [DNSRecord(name=n, a=ip, owner=tag) for n in sorted(names)]
        step(15, f"DNS api / *.apps.{zone} -> {ip} on the group router")
        group_service.update_owned(db, group.id, records)

        step(20, "ClusterDeployment, AgentClusterInstall, InfraEnv, BareMetalHost on the hub")
        key_dir = openshift_service.cluster_dir(hub.name) / "spokes" / name
        key_dir.mkdir(parents=True, exist_ok=True)
        ssh_pub = openshift_service.ssh_keypair(key_dir)
        creds = bmc_service.credentials(group.name)
        imageset = f"img{version}-x86-64"
        runner.apply_retry([{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}], "Namespace")
        for secret in (
            {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
             "metadata": {"name": "pull-secret", "namespace": name},
             "stringData": {".dockerconfigjson": openshift_service.pull_secret()}},
            {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
             "metadata": {"name": f"{member['name']}-bmc", "namespace": name},
             "stringData": {"username": creds["username"], "password": creds["password"]}},
        ):  # oc create: no secret in a last-applied annotation
            runner.oc(["delete", "secret", secret["metadata"]["name"], "-n", name, "--ignore-not-found"], 30)
            out = runner.oc(["create", "-f", "-"], 60, stdin=json.dumps(secret))
            if out["exitcode"] != 0:
                raise RuntimeError(f"Secret {secret['metadata']['name']}: {(out['stderr'] or '').strip()[-300:]}")
        cidr = (group.spec or {}).get("cidr")
        runner.apply_retry([
            {"apiVersion": "hive.openshift.io/v1", "kind": "ClusterImageSet", "metadata": {"name": imageset},
             "spec": {"releaseImage": f"quay.io/openshift-release-dev/ocp-release:{version}-x86_64"}},
            {"apiVersion": "extensions.hive.openshift.io/v1beta1", "kind": "AgentClusterInstall",
             "metadata": {"name": name, "namespace": name},
             "spec": {"clusterDeploymentRef": {"name": name}, "imageSetRef": {"name": imageset},
                      "networking": {"machineNetwork": [{"cidr": cidr}],
                                     "clusterNetwork": [{"cidr": "10.128.0.0/14", "hostPrefix": 23}],
                                     "serviceNetwork": ["172.30.0.0/16"], "networkType": "OVNKubernetes"},
                      "provisionRequirements": {"controlPlaneAgents": 1}, "sshPublicKey": ssh_pub}},
            {"apiVersion": "hive.openshift.io/v1", "kind": "ClusterDeployment",
             "metadata": {"name": name, "namespace": name},
             "spec": {"baseDomain": domain, "clusterName": name,
                      "clusterInstallRef": {"group": "extensions.hive.openshift.io", "kind": "AgentClusterInstall",
                                            "name": name, "version": "v1beta1"},
                      "platform": {"agentBareMetal": {"agentSelector": {"matchLabels": {"vmm-spoke": name}}}},
                      "pullSecretRef": {"name": "pull-secret"}}},
            {"apiVersion": "agent-install.openshift.io/v1beta1", "kind": "InfraEnv",
             "metadata": {"name": name, "namespace": name},
             "spec": {"clusterRef": {"name": name, "namespace": name}, "pullSecretRef": {"name": "pull-secret"},
                      "sshAuthorizedKey": ssh_pub, "agentLabels": {"vmm-spoke": name}}},
            {"apiVersion": "metal3.io/v1alpha1", "kind": "BareMetalHost",
             "metadata": {"name": member["name"], "namespace": name,
                          "labels": {"infraenvs.agent-install.openshift.io": name},
                          "annotations": {"inspect.metal3.io": "disabled",
                                          "bmac.agent-install.openshift.io/hostname": member["name"],
                                          "bmac.agent-install.openshift.io/role": "master"}},
             "spec": {"online": True, "bootMACAddress": member["mac"], "bootMode": "legacy",
                      "automatedCleaningMode": "disabled",
                      "bmc": {"address": host["redfish_url"], "credentialsName": f"{member['name']}-bmc",
                              "disableCertificateVerification": True}}},
            {"apiVersion": "cluster.open-cluster-management.io/v1", "kind": "ManagedCluster",
             "metadata": {"name": name, "labels": {"cloud": "auto-detect", "vendor": "auto-detect",
                                                   "vmm-cluster-type": "spoke"}},
             "spec": {"hubAcceptsClient": True}},
            {"apiVersion": "agent.open-cluster-management.io/v1", "kind": "KlusterletAddonConfig",
             "metadata": {"name": name, "namespace": name},
             "spec": {"applicationManager": {"enabled": True}, "policyController": {"enabled": True},
                      "searchCollector": {"enabled": True}, "certPolicyController": {"enabled": True}}},
        ], "spoke resources", timeout=5 * 60)

        # discovery: metal3 boots the ISO through the BMC, the agent registers
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        last = ""
        while True:
            bmh = self._get(runner, ["get", "bmh", member["name"], "-n", name])
            bstatus = bmh.get("status") or {}
            agents = self._get(runner, ["get", "agents", "-n", name]).get("items", [])
            if agents:
                break
            msg = (f"BareMetalHost {bstatus.get('provisioning', {}).get('state') or 'registering'}"
                   + (f", {bstatus.get('operationalStatus')}" if bstatus.get("operationalStatus") else "")
                   + (f": {bstatus.get('errorMessage')}" if bstatus.get("errorMessage") else "")
                   + "; waiting for the discovery agent")
            if msg != last:
                step(30, msg)
                last = msg
            if time.monotonic() > deadline:
                raise TimeoutError(f"No agent registered after {DISCOVERY_TIMEOUT // 60} min ({msg}). The machine's "
                                   "console shows whether it booted the discovery ISO; the BMC emulator log is "
                                   f"{bmc_service.group_dir(group.name) / 'bmc-emulator.log'}.")
            svc._sleep(task, 20)
        step(40, f"agent {agents[0]['metadata']['name'][:8]} registered, installing")
        # install
        deadline = time.monotonic() + INSTALL_TIMEOUT
        while True:
            aci = self._get(runner, ["get", "agentclusterinstall", name, "-n", name])
            conds = {c.get("type"): c for c in (aci.get("status") or {}).get("conditions") or []}
            done = conds.get("Completed") or {}
            failed = conds.get("Failed") or {}
            if done.get("status") == "True":
                break
            if failed.get("status") == "True":
                raise RuntimeError(f"Spoke install failed: {failed.get('message')}")
            pct = ((aci.get("status") or {}).get("progress") or {}).get("totalPercentage") or 0
            state = (aci.get("status") or {}).get("debugInfo", {}).get("state") or "preparing"
            info = (aci.get("status") or {}).get("debugInfo", {}).get("stateInfo") or ""
            msg = f"install {state} {pct}%" + (f": {info[:150]}" if info else "")
            if msg != last:
                step(40 + int(pct) * 55 // 100, msg)
                last = msg
            if time.monotonic() > deadline:
                raise TimeoutError(f"Spoke not installed after {INSTALL_TIMEOUT // 60} min ({msg})")
            svc._sleep(task, 30)
        step(97, "installed; waiting for the ManagedCluster to become Available")
        deadline = time.monotonic() + 20 * 60
        while time.monotonic() < deadline:
            from app.services.acm import conditions_state
            st = conditions_state(self._get(runner, ["get", "managedcluster", name]))
            if st.get("available"):
                break
            svc._sleep(task, 20)
        self._save(db, hub, name, state="installed", message=None)

    @staticmethod
    def _get(runner: Any, args: List[str]) -> Dict[str, Any]:
        try:
            return runner.oc_json(args, 30)
        except (RuntimeError, ValueError):
            return {}

    # ------------------------------------------------------------------------------------------------ views

    def status(self, db: Session, hub: Cluster) -> List[Dict[str, Any]]:
        out = []
        runner = self.acm._runner(hub) if self.svc.to_dict(db, hub)["status"] == "ready" else None
        for e in self._entries(hub):
            entry = dict(e)
            if runner is not None and e.get("state") not in ("pending",):
                bmh = self._get(runner, ["get", "bmh", e["machine"], "-n", e["name"]])
                entry["bmh_state"] = ((bmh.get("status") or {}).get("provisioning") or {}).get("state")
                aci = self._get(runner, ["get", "agentclusterinstall", e["name"], "-n", e["name"]])
                entry["install_state"] = ((aci.get("status") or {}).get("debugInfo") or {}).get("state")
                entry["progress"] = ((aci.get("status") or {}).get("progress") or {}).get("totalPercentage")
            entry["console_url"] = (f"https://console-openshift-console.apps.{e['name']}."
                                    f"{self._domain(db, e.get('group_id'))}")
            out.append(entry)
        return out

    @staticmethod
    def _domain(db: Session, group_id: Optional[int]) -> str:
        group = db.query(Group).filter(Group.id == group_id).first() if group_id else None
        return ((group.spec or {}).get("domain") or f"{group.name}.lab") if group else "lab"

    def kubeconfig(self, db: Session, hub: Cluster, name: str) -> str:
        """The spoke's admin kubeconfig (hive secret <name>-admin-kubeconfig on the hub)"""
        import base64
        if not any(e["name"] == name for e in self._entries(hub)):
            raise LookupError(f"No spoke {name} on {hub.name}")
        data = self._get(self.acm._runner(hub), ["get", "secret", f"{name}-admin-kubeconfig", "-n", name]).get("data")
        if not data or "kubeconfig" not in data:
            raise ValueError("The spoke has no kubeconfig yet (written by the install)")
        return base64.b64decode(data["kubeconfig"]).decode()

    def delete(self, db: Session, hub: Cluster, name: str) -> Task:
        entry = next((e for e in self._entries(hub) if e["name"] == name), None)
        if entry is None:
            raise LookupError(f"No spoke {name} on {hub.name}")
        if self.svc.to_dict(db, hub)["status"] != "ready":
            raise ValueError(f"Hub {hub.name} is not ready")

        def run(db: Session, task: Task, hub_id: int) -> Dict[str, Any]:
            def body(hub: Cluster) -> Dict[str, Any]:
                from app.services.group_service import group_service, member_vm_name
                from app.libvirt_client import libvirt_client
                runner = self.acm._runner(hub, task, db)
                self.svc._check(task, hub, db, 10, f"Deleting spoke {name} (ManagedCluster, install, BareMetalHost)")
                runner.oc(["delete", "managedcluster", name, "--ignore-not-found", "--wait=false"], 60)
                runner.oc(["delete", "clusterdeployment", name, "-n", name, "--ignore-not-found", "--wait=true",
                           "--timeout=10m"], 660)
                runner.oc(["delete", "bmh", entry["machine"], "-n", name, "--ignore-not-found", "--wait=true",
                           "--timeout=5m"], 330)
                runner.oc(["delete", "namespace", name, "--ignore-not-found", "--wait=false"], 60)
                tag = owner(name)

                def records(spec) -> None:
                    spec.router.dns.records = [r for r in spec.router.dns.records if r.owner != tag]
                try:
                    group_service.update_owned(db, entry["group_id"], records)
                except ValueError as e:
                    logger.warning(f"Spoke {name} DNS cleanup: {e}")
                group = db.query(Group).filter(Group.id == entry["group_id"]).first()
                if group is not None:
                    try:
                        libvirt_client.stop_vm(member_vm_name(group.name, entry["machine"]), force=True)
                    except Exception:
                        pass
                self._drop(db, hub, name)
                self.svc._set_status(db, hub, "ready", None)
                return {"deleted": name}
            return self.svc._guarded(db, task, hub_id, body, failed_status="ready")
        return self.svc._run_task(db, hub, "acm_spoke_delete", f"Delete spoke {name} of", run)


spoke_service = SpokeService()
