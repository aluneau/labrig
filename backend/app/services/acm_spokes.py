"""ACM hub provisions a single-node OpenShift "spoke" on emulated bare metal (docs/openshift.md "Provision a spoke").

A spoke is a cluster of the app like the others (a `clusters` row, type openshift, in the hub's lab group) whose
`spec.openshift.provisioner = {kind: acm, hub_id, hub_name, machine}`: kubeconfig, logs, the MCP oc tool, the
Clusters list work as usual. Its one node is a BMC member of the hub's group (bmc_service: Redfish emulator on the
host), so it resolves and reaches the hub's routes through the group router like the hub itself. On the hub
(central infrastructure management, the customer's ZTP building blocks):
  Provisioning (metal3 on a non-bare-metal hub: provisioningNetwork Disabled, watchAllNamespaces) +
  AgentServiceConfig (assisted service, volumes on the hub's default StorageClass), then per spoke:
  Namespace, pull secret, BMC secret, ClusterImageSet, ClusterDeployment + AgentClusterInstall (SNO, user-managed
  networking), InfraEnv, BareMetalHost (redfish-virtualmedia, bootMode legacy, inspection off: the assisted
  agent reports the hardware), ManagedCluster + KlusterletAddonConfig (imported once installed).
metal3 inserts the discovery ISO through the BMC (virtual media) and powers the machine on; the agent registers,
the assisted service installs. The group router serves api / api-int / *.apps.<spoke>.<domain> -> the machine and
forwards a free port (6444...) of its uplink address to the spoke API: the host's kubeconfig (from the hub's
<spoke>-admin-kubeconfig secret) points there (tls-server-name api.<spoke>.<domain>), like the hub's.
The machine VM carries the cluster metadata (provisioner acm, hub), so sync_clusters adopts the spoke back.
Deleting the spoke goes through the hub (ClusterDeployment, BareMetalHost, ManagedCluster); the machine stays.
"""
import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import quoteattr

from sqlalchemy.orm import Session

from app.libvirt_client import libvirt_client
from app.models import Cluster, ClusterNode, Group, Task

logger = logging.getLogger(__name__)

MACHINE_NS = "openshift-machine-api"
MCE_NS = "multicluster-engine"
CIM_TIMEOUT = 25 * 60
DISCOVERY_TIMEOUT = 40 * 60
INSTALL_TIMEOUT = 150 * 60


def provisioner(cluster: Cluster) -> Optional[Dict[str, Any]]:
    """{kind: acm, hub_id, hub_name, machine} for a spoke provisioned by an ACM hub"""
    p = (((cluster.spec or {}).get("openshift") or {}).get("provisioner"))
    return p if isinstance(p, dict) and p.get("kind") == "acm" else None


class SpokeService:
    @property
    def acm(self):
        from app.services.acm import acm_service
        return acm_service

    @property
    def svc(self):
        from app.services.cluster_service import cluster_service
        return cluster_service

    @property
    def installer(self):
        from app.services.openshift_installer import openshift_installer
        return openshift_installer

    def spokes_of(self, db: Session, hub: Cluster) -> List[Cluster]:
        return [c for c in db.query(Cluster).filter(Cluster.type == "openshift").all()
                if (provisioner(c) or {}).get("hub_id") == hub.id]

    def hub_of(self, db: Session, spoke: Cluster) -> Optional[Cluster]:
        p = provisioner(spoke)
        return self.svc.get_cluster(db, p["hub_id"]) if p else None

    # ------------------------------------------------------------------------------------------------ hub side

    @staticmethod
    def os_image(version: str) -> Dict[str, Any]:
        """The assisted service's RHCOS live ISO for `version` (x86_64 only: its default list is every architecture
        and release, ~15 ISOs that overflow the image store)"""
        import subprocess
        from app.services.openshift_service import openshift_service
        installer = openshift_service.bin_dir(version) / "openshift-install"
        if not installer.is_file():
            openshift_service.ensure_binaries(version, lambda m, p: None, lambda: False)
        out = subprocess.run([str(installer), "coreos", "print-stream-json"], capture_output=True, text=True,
                             timeout=120)
        metal = json.loads(out.stdout)["architectures"]["x86_64"]["artifacts"]["metal"]
        return {"openshiftVersion": ".".join(version.split(".")[:2]), "version": metal["release"],
                "url": metal["formats"]["iso"]["disk"]["location"], "cpuArchitecture": "x86_64"}

    def ensure_cim(self, runner: Any, hub: Cluster, version: str) -> None:
        """metal3 + assisted service on the hub (idempotent). The hub router forwards Ironic's image ports
        (6180 http / 6183 https, on the hub nodes' host network) from its uplink address: the BMC emulator on the
        host downloads virtual media from there (it has no route to the group network)."""
        from app.services.cluster_network import GroupClusterNetwork
        network = GroupClusterNetwork(hub.group_id, hub.name, owned=bool(hub.group_owned))
        masters = [n.ip for n in hub.nodes if n.role == "ctlplane" and n.ip]
        for port in (6180, 6183):
            network.publish_lb(f"{hub.name}-ironic-{port}", port, [f"{ip}:{port}" for ip in masters])
        network.commit()
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
        image = self.os_image(version)
        try:
            current = runner.oc_json(["get", "agentserviceconfig", "agent"], 30).get("spec", {}).get("osImages") or []
        except (RuntimeError, ValueError):
            current = []
        images = [i for i in current if i.get("openshiftVersion") != image["openshiftVersion"]] + [image]
        runner.apply_retry([{"apiVersion": "agent-install.openshift.io/v1beta1", "kind": "AgentServiceConfig",
                             "metadata": {"name": "agent"},
                             "spec": {"databaseStorage": volume("10Gi"), "filesystemStorage": volume("20Gi"),
                                      "imageStorage": volume("20Gi"), "osImages": images}}],
                           "AgentServiceConfig", timeout=5 * 60)
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

    def provision(self, db: Session, hub: Cluster, name: str, machine: str, version: Optional[str]) -> Cluster:
        """POST /clusters/{hub}/acm/spokes: the spoke's cluster row + its provisioning task"""
        from app.services import bmc_service
        from app.services.group_service import member_vm_name
        from app.services.openshift_service import openshift_service
        from app.schemas import TaskCreate
        from app.services.task_service import task_service
        if not self.acm.is_hub(hub) or self.acm._addon(hub).get("state") != "done":
            raise ValueError(f"ACM is not installed on {hub.name}: enable it first")
        if self.svc.to_dict(db, hub)["status"] != "ready":
            raise ValueError(f"Hub {hub.name} is not ready")
        if db.query(Cluster).filter(Cluster.name == name).first() is not None:
            raise ValueError(f"A cluster named {name} already exists")
        group = db.query(Group).filter(Group.id == hub.group_id).first()
        gspec = group.spec or {}
        member = next((m for m in gspec.get("members") or [] if m["name"] == machine), None)
        if member is None or not member.get("bmc"):
            raise ValueError(f"{machine} is not a BMC member of the hub's group {group.name}: add spoke machines "
                             "first (group page, BMC tab)")
        if int(member.get("memory") or 0) < 16384 or int(member.get("vcpu") or 0) < 8:
            raise ValueError(f"{machine} is too small for single-node OpenShift (16 GiB / 8 vCPU minimum)")
        if any((provisioner(c) or {}).get("machine") == machine for c in self.spokes_of(db, hub)):
            raise ValueError(f"{machine} is already used by another spoke")
        status = bmc_service.status(group)
        if not status["running"] or bmc_service.available():
            raise ValueError(f"The group's BMC emulator is not running: {status.get('error') or 'see the group page'}")
        host = next(h for h in status["members"] if h["name"] == machine)
        if not host["redfish_url"]:
            raise ValueError(f"{machine}: its VM is missing")
        version = version or hub.version
        openshift_service.pull_secret()
        domain = gspec.get("domain") or f"{group.name}.lab"
        vm = member_vm_name(group.name, machine)
        spec = {"name": name, "type": "openshift", "ctlplanes": 1, "workers": 0, "addons": [],
                "ctlplane": {"memory": member.get("memory"), "vcpu": member.get("vcpu"),
                             "disk_size": member.get("disk_size")},
                "cidr": gspec.get("cidr"), "image": f"OpenShift {version}",
                "openshift": {"version": version, "topology": "sno", "storage": "none",
                              "provisioner": {"kind": "acm", "hub_id": hub.id, "hub_name": hub.name,
                                              "machine": machine}}}
        cluster = Cluster(name=name, type="openshift", version=version, network=f"vmm-g-{group.name}",
                          network_owned=False, group_id=group.id, group_owned=False, domain=domain, spec=spec,
                          status="provisioning", status_message=f"Queued: provisioned by ACM hub {hub.name}")
        cluster.nodes.append(ClusterNode(name=vm, role="ctlplane", ip=member.get("ip"), mac=member.get("mac")))
        db.add(cluster)
        db.commit()
        db.refresh(cluster)
        self.installer.save_spec(cluster)
        self._set_metadata(cluster, hub, group)
        self.svc._publish(cluster)
        task = task_service.start(db, TaskCreate(
            name=f"Provision spoke {name} from ACM hub {hub.name}", type="cluster_create", target_type="cluster",
            target_id=cluster.id, target_name=name,
            description=f"OpenShift {version}, single node on {machine} (BMC), by ACM hub {hub.name}",
        ), self._run, cluster.id, hub.id, dict(member), dict(host))
        cluster.task_id = task.id
        db.commit()
        return cluster

    @staticmethod
    def _set_metadata(cluster: Cluster, hub: Cluster, group: Group) -> None:
        """Cluster metadata on the machine VM: sync_clusters adopts the spoke from it (+ spec.json)"""
        from app.services.cluster_service import METADATA_KEY, METADATA_URI
        node = cluster.nodes[0]
        attrs = {"name": cluster.name, "type": "openshift", "role": "ctlplane", "ip": node.ip or "",
                 "domain": cluster.domain, "network": cluster.network, "owned": "no", "version": cluster.version or "",
                 "group": group.name, "group_owned": "no", "provisioner": "acm", "hub": hub.name}
        xml = "<cluster " + " ".join(f"{k}={quoteattr(str(v))}" for k, v in attrs.items()) + "/>"
        try:
            libvirt_client.set_domain_metadata(node.name, xml, METADATA_KEY, METADATA_URI)
        except Exception as e:  # the VM is the group's: never fail the request for this
            logger.warning(f"Spoke {cluster.name}: metadata on {node.name}: {e}")

    def _run(self, db: Session, task: Task, cluster_id: int, hub_id: int, member: Dict[str, Any],
             host: Dict[str, Any]) -> Dict[str, Any]:
        def body(cluster: Cluster) -> Dict[str, Any]:
            hub = self.svc.get_cluster(db, hub_id)
            if hub is None:
                raise ValueError("The hub was deleted")
            self._provision(db, task, hub, cluster, member, host)
            self.svc._set_status(db, cluster, "ready", None)
            return {"spoke": cluster.name}
        return self.svc._guarded(db, task, cluster_id, body)

    def _provision(self, db: Session, task: Task, hub: Cluster, cluster: Cluster, member: Dict[str, Any],
                   host: Dict[str, Any]) -> None:
        from app.services import bmc_service
        from app.services.cluster_network import GroupClusterNetwork
        from app.services.openshift_addons import AddonRunner
        from app.services.openshift_service import openshift_service
        svc = self.svc
        name, version = cluster.name, cluster.version
        group = db.query(Group).filter(Group.id == cluster.group_id).first()

        def step(pct: int, message: str, phase: str = "preparing", progress: Optional[int] = None) -> None:
            svc._check(task, cluster, db, pct, message)
            self.installer._set_live(cluster.id, phase=phase, progress=progress, assisted_info=message)
        runner = AddonRunner(hub.name, hub.version, lambda s: svc._sleep(task, s),
                             lambda m: svc._check(task, cluster, db, None, m))

        step(5, f"Hub {hub.name}: central infrastructure management (metal3, assisted service)")
        self.ensure_cim(runner, hub, version)

        zone = f"{name}.{cluster.domain}"
        ip = member["ip"]
        network = GroupClusterNetwork(group.id, name, owned=False)
        port = network.free_lb_port(6444)
        network.publish(ip, [f"api.{zone}", f"api-int.{zone}", f"*.apps.{zone}"])
        network.publish_lb(f"{name}-api", port, [f"{ip}:6443"])
        step(15, f"Router: api / *.apps.{zone} -> {ip}, API on {network.uplink_ip()}:{port}")
        network.commit()
        cluster.api_ip = network.uplink_ip()
        self.installer._update_spec(db, cluster, api_port=port)

        step(20, "Hub: ClusterDeployment, AgentClusterInstall, InfraEnv, BareMetalHost")
        key_dir = openshift_service.cluster_dir(name)
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
        runner.apply_retry([
            {"apiVersion": "hive.openshift.io/v1", "kind": "ClusterImageSet", "metadata": {"name": imageset},
             "spec": {"releaseImage": f"quay.io/openshift-release-dev/ocp-release:{version}-x86_64"}},
            {"apiVersion": "extensions.hive.openshift.io/v1beta1", "kind": "AgentClusterInstall",
             "metadata": {"name": name, "namespace": name},
             "spec": {"clusterDeploymentRef": {"name": name}, "imageSetRef": {"name": imageset},
                      "networking": {"machineNetwork": [{"cidr": (group.spec or {}).get("cidr")}],
                                     "clusterNetwork": [{"cidr": "10.128.0.0/14", "hostPrefix": 23}],
                                     "serviceNetwork": ["172.30.0.0/16"], "networkType": "OVNKubernetes"},
                      "provisionRequirements": {"controlPlaneAgents": 1}, "sshPublicKey": ssh_pub}},
            {"apiVersion": "hive.openshift.io/v1", "kind": "ClusterDeployment",
             "metadata": {"name": name, "namespace": name},
             "spec": {"baseDomain": cluster.domain, "clusterName": name,
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
                                          "bmac.agent-install.openshift.io/hostname": cluster.nodes[0].name,
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
            msg = (f"BareMetalHost {(bstatus.get('provisioning') or {}).get('state') or 'registering'}"
                   + (f", {bstatus.get('operationalStatus')}" if bstatus.get("operationalStatus") else "")
                   + (f": {bstatus.get('errorMessage')}" if bstatus.get("errorMessage") else "")
                   + "; waiting for the discovery agent")
            if msg != last:
                step(30, msg, "booting")
                last = msg
            if time.monotonic() > deadline:
                raise TimeoutError(f"No agent registered after {DISCOVERY_TIMEOUT // 60} min ({msg}). The machine's "
                                   "console shows whether it booted the discovery ISO; the BMC emulator log is "
                                   f"{bmc_service.group_dir(group.name) / 'bmc-emulator.log'}.")
            svc._sleep(task, 20)
        step(40, f"Agent {agents[0]['metadata']['name'][:8]} registered: installing", "installing")
        deadline = time.monotonic() + INSTALL_TIMEOUT
        while True:
            aci = self._get(runner, ["get", "agentclusterinstall", name, "-n", name])
            conds = {c.get("type"): c for c in (aci.get("status") or {}).get("conditions") or []}
            if (conds.get("Completed") or {}).get("status") == "True":
                break
            if (conds.get("Failed") or {}).get("status") == "True":
                raise RuntimeError(f"Spoke install failed: {conds['Failed'].get('message')}")
            pct = int(((aci.get("status") or {}).get("progress") or {}).get("totalPercentage") or 0)
            debug = (aci.get("status") or {}).get("debugInfo") or {}
            msg = f"Install {debug.get('state') or 'preparing'} {pct}%" + (
                f": {debug['stateInfo'][:150]}" if debug.get("stateInfo") else "")
            if msg != last:
                step(40 + pct * 50 // 100, msg, "installing", pct)
                last = msg
            if time.monotonic() > deadline:
                raise TimeoutError(f"Spoke not installed after {INSTALL_TIMEOUT // 60} min ({msg})")
            svc._sleep(task, 30)
        step(92, "Installed: kubeconfig from the hub", "finalizing", 100)
        self.write_kubeconfig(db, hub, cluster)
        step(95, "Waiting for the ManagedCluster to become Available", "finalizing", 100)
        deadline = time.monotonic() + 20 * 60
        from app.services.acm import conditions_state
        while time.monotonic() < deadline:
            if conditions_state(self._get(runner, ["get", "managedcluster", name])).get("available"):
                break
            svc._sleep(task, 20)
        self.installer._set_live(cluster.id, phase="ready")

    def write_kubeconfig(self, db: Session, hub: Cluster, cluster: Cluster) -> str:
        """The hub's <spoke>-admin-kubeconfig, pointed at the router's forwarded port (like the hub's own); the
        kubeadmin password next to it"""
        from app.services.openshift_addons import AddonRunner
        from app.services.openshift_service import openshift_service
        import os
        runner = AddonRunner(hub.name, hub.version, time.sleep, lambda m: None)
        name = cluster.name
        data = self._get(runner, ["get", "secret", f"{name}-admin-kubeconfig", "-n", name]).get("data") or {}
        if "kubeconfig" not in data:
            raise ValueError("The spoke has no kubeconfig yet (written by the install)")
        port = int((cluster.spec or {}).get("api_port") or 6443)
        text = openshift_service.kubeconfig_for_host(base64.b64decode(data["kubeconfig"]).decode(), name,
                                                     f"https://{cluster.api_ip}:{port}", f"api.{name}.{cluster.domain}")
        workdir = openshift_service.cluster_dir(name)
        for fname, content in (("host-kubeconfig", text),):
            fd = os.open(str(workdir / fname), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(content)
        pw = (self._get(runner, ["get", "secret", f"{name}-admin-password", "-n", name]).get("data") or {})
        if pw.get("password"):
            (workdir / "auth").mkdir(exist_ok=True)
            fd = os.open(str(workdir / "auth" / "kubeadmin-password"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(base64.b64decode(pw["password"]).decode())
        cluster.kubeconfig = text
        db.commit()
        return text

    @staticmethod
    def _get(runner: Any, args: List[str]) -> Dict[str, Any]:
        try:
            return runner.oc_json(args, 30)
        except (RuntimeError, ValueError):
            return {}

    # ------------------------------------------------------------------------------------------------ views

    def summary(self, db: Session, hub: Cluster) -> List[Dict[str, Any]]:
        """The hub's spokes for its ACM tab (cluster rows; live BareMetalHost / install state when the hub is up)"""
        out = []
        ready = self.svc.to_dict(db, hub)["status"] == "ready"
        runner = self.acm._runner(hub) if ready else None
        for c in self.spokes_of(db, hub):
            p = provisioner(c) or {}
            d = self.svc.to_dict(db, c)
            entry = {"id": c.id, "name": c.name, "machine": p.get("machine"), "version": c.version,
                     "state": d["status"], "message": d["status_message"],
                     "ip": c.nodes[0].ip if c.nodes else None, "console_url": d.get("console_url")}
            if runner is not None:
                bmh = self._get(runner, ["get", "bmh", p.get("machine") or "-", "-n", c.name])
                entry["bmh_state"] = ((bmh.get("status") or {}).get("provisioning") or {}).get("state")
                aci = self._get(runner, ["get", "agentclusterinstall", c.name, "-n", c.name])
                entry["install_state"] = ((aci.get("status") or {}).get("debugInfo") or {}).get("state")
                entry["progress"] = ((aci.get("status") or {}).get("progress") or {}).get("totalPercentage")
            out.append(entry)
        return out

    # ------------------------------------------------------------------------------------------------ delete

    def release(self, db: Session, cluster: Cluster) -> None:
        """cluster_service.delete_cluster of a spoke: its objects on the hub (no wait: the hub finishes the
        cleanup), the machine powered off and its cluster metadata removed (the VM stays, the group's)"""
        from app.services.cluster_service import METADATA_KEY, METADATA_URI
        hub = self.hub_of(db, cluster)
        p = provisioner(cluster) or {}
        if hub is not None and self.svc.to_dict(db, hub)["status"] == "ready":
            runner = self.acm._runner(hub)
            for args in (["delete", "managedcluster", cluster.name],
                         ["delete", "clusterdeployment", cluster.name, "-n", cluster.name],
                         ["delete", "bmh", p.get("machine") or "-", "-n", cluster.name],
                         ["delete", "namespace", cluster.name]):
                try:
                    runner.oc(args + ["--ignore-not-found", "--wait=false"], 60)
                except (RuntimeError, ValueError) as e:
                    logger.warning(f"Spoke {cluster.name}: oc {' '.join(args[:2])} on {hub.name}: {e}")
        elif hub is not None:
            logger.warning(f"Spoke {cluster.name}: hub {hub.name} not ready, its objects stay on the hub")
        for node in cluster.nodes:
            try:
                libvirt_client.stop_vm(node.name, force=True)
            except Exception:
                pass
            try:
                libvirt_client.set_domain_metadata(node.name, None, METADATA_KEY, METADATA_URI)
            except Exception as e:
                logger.warning(f"Spoke {cluster.name}: metadata of {node.name}: {e}")


spoke_service = SpokeService()
