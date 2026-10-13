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

from app.config import settings
from app.libvirt_client import libvirt_client
from app.models import Cluster, ClusterNode, Group, Task

logger = logging.getLogger(__name__)

MACHINE_NS = "openshift-machine-api"
MCE_NS = "multicluster-engine"
CIM_TIMEOUT = 25 * 60
DISCOVERY_TIMEOUT = 40 * 60
INSTALL_TIMEOUT = 150 * 60


def provisioner(cluster: Cluster) -> Optional[Dict[str, Any]]:
    """{kind: acm, hub_id, hub_name, machine|host_id} for a spoke provisioned by an ACM hub"""
    p = (((cluster.spec or {}).get("openshift") or {}).get("provisioner"))
    return p if isinstance(p, dict) and p.get("kind") == "acm" else None


def real_bmh_id(cluster: Cluster) -> Optional[int]:
    """The id of the REAL BareMetalHost a spoke is built on, or None for an emulated-BMC (VM) spoke."""
    p = provisioner(cluster) or {}
    hid = p.get("host_id")
    return int(hid) if hid is not None else None


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
        if hub.group_id:
            # Emulated BMC (VM) hub: the emulator on the host has no route to the group network, so forward
            # Ironic's image ports from the router's uplink. A bare-metal hub has no group/router — the real
            # BMC is on a routable network and reaches the hub's apps/image service directly.
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

    def provision(self, db: Session, hub: Cluster, name: str, machine: Optional[str],
                  version: Optional[str], host_id: Optional[int] = None,
                  net: Optional[Dict[str, Any]] = None, gitops: bool = False) -> Cluster:
        """POST /clusters/{hub}/acm/spokes: the spoke's cluster row + its provisioning task. `machine` =
        an emulated-BMC member of the hub's group; `host_id` + `net` = a REAL BareMetalHost (physical server).
        `gitops` = deploy "the realistic way": commit the manifests to the in-lab git server and let the hub's
        Argo CD sync them, instead of the app applying the CRs directly (real BareMetalHost spokes only)."""
        if not self.acm.is_hub(hub) or self.acm._addon(hub).get("state") != "done":
            raise ValueError(f"ACM is not installed on {hub.name}: enable it first")
        if self.svc.to_dict(db, hub)["status"] != "ready":
            raise ValueError(f"Hub {hub.name} is not ready")
        if db.query(Cluster).filter(Cluster.name == name).first() is not None:
            raise ValueError(f"A cluster named {name} already exists")
        if gitops and host_id is None:
            raise ValueError("GitOps deployment is only supported for real BareMetalHost spokes")
        if host_id is not None:
            return self._provision_real_host(db, hub, name, host_id, net or {}, version, gitops)
        return self._provision_member(db, hub, name, machine, version)

    def _provision_member(self, db: Session, hub: Cluster, name: str, machine: str,
                          version: Optional[str]) -> Cluster:
        """Spoke on an emulated-BMC member of the hub's lab group (the original flow)."""
        from app.services import bmc_service
        from app.services.group_service import member_vm_name
        from app.services.openshift_service import openshift_service
        from app.schemas import TaskCreate
        from app.services.task_service import task_service
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

    # ---------------------------------------------------------------------- real bare-metal host spoke

    def _provision_real_host(self, db: Session, hub: Cluster, name: str, host_id: int,
                             net: Dict[str, Any], version: Optional[str], gitops: bool = False) -> Cluster:
        """Spoke on a REAL BareMetalHost (physical server). The hub is bare-metal / groupless (no router):
        the node's api / *.apps resolve through the host DNS resolver, and the real BMC fetches the discovery
        ISO straight from the hub's assisted-image-service (reachable on its routable network)."""
        import ipaddress
        from app.services.baremetal_service import baremetal_service
        from app.services.openshift_service import openshift_service
        from app.schemas import TaskCreate
        from app.services.task_service import task_service
        host = baremetal_service.get_host(db, host_id)
        if host is None:
            raise ValueError(f"Bare-metal host {host_id} not found")
        if host.cluster_name:
            raise ValueError(f"Host '{host.name}' already backs cluster '{host.cluster_name}'")
        if host.status not in ("available", "error"):
            raise ValueError(f"Host '{host.name}' is {host.status}: it must be available")
        if not host.boot_mac:
            raise ValueError(f"Host '{host.name}' has no known boot MAC: refresh its inventory or set it first")
        if any(real_bmh_id(c) == host.id for c in self.spokes_of(db, hub)):
            raise ValueError(f"Host '{host.name}' is already used by another spoke")
        ip = (net.get("ip") or "").strip()
        gateway = (net.get("gateway") or "").strip()
        prefix = int(net.get("prefix") or 0)
        dns = [d.strip() for d in (net.get("dns") or []) if d.strip()]
        if not ip or not gateway or not prefix:
            raise ValueError("A real bare-metal spoke needs the node's static network (ip, prefix, gateway, dns)")
        try:
            ipaddress.ip_address(ip)
            ipaddress.ip_address(gateway)
            for d in dns:
                ipaddress.ip_address(d)
            machine_net = str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))
        except ValueError as e:
            raise ValueError(f"Spoke networking: {e}")
        if ipaddress.ip_address(ip) not in ipaddress.ip_network(machine_net):
            raise ValueError(f"The node IP {ip} is not in its network {machine_net}")
        version = version or hub.version
        openshift_service.pull_secret()
        inv = host.inventory or {}
        node_name = f"{name}-ctlplane-0"
        spec = {"name": name, "type": "openshift", "ctlplanes": 1, "workers": 0, "addons": [],
                "ctlplane": {"memory": int((inv.get("memory_gib") or 0) * 1024) or None,
                             "vcpu": inv.get("cpu_count")},
                "cidr": machine_net, "image": f"OpenShift {version}",
                "openshift": {"version": version, "topology": "sno", "storage": "none",
                              "provisioner": {"kind": "acm", "hub_id": hub.id, "hub_name": hub.name,
                                              "host_id": host.id, "host_name": host.name}},
                "gitops": bool(gitops),
                "spoke_net": {"ip": ip, "prefix": prefix, "gateway": gateway, "dns": dns,
                              "mac": host.boot_mac, "machine_network": machine_net,
                              "root_device": net.get("root_device"),
                              "ntp": list(net.get("ntp") or []) or self.installer._host_ntp_servers()}}
        cluster = Cluster(name=name, type="openshift", version=version, network="bare-metal",
                          network_owned=False, group_id=None, group_owned=False, domain=hub.domain, spec=spec,
                          api_ip=ip, status="provisioning",
                          status_message=f"Queued: provisioned by ACM hub {hub.name}")
        cluster.nodes.append(ClusterNode(name=node_name, role="ctlplane", ip=ip, mac=host.boot_mac))
        db.add(cluster)
        db.commit()
        db.refresh(cluster)
        self.installer.save_spec(cluster)
        host.cluster_name = name
        host.status = "provisioning"
        host.status_message = None
        db.commit()
        self.svc._publish(cluster)
        task = task_service.start(db, TaskCreate(
            name=f"Provision spoke {name} from ACM hub {hub.name}", type="cluster_create",
            target_type="cluster", target_id=cluster.id, target_name=name,
            description=f"OpenShift {version}, single node on real host {host.name}, by ACM hub {hub.name}",
        ), self._run_real, cluster.id, hub.id, host.id)
        cluster.task_id = task.id
        db.commit()
        from app.services.gitops_service import gitops_service
        gitops_service.sync_spoke(db, cluster)  # record the spoke in the GitOps repo (source of truth)
        return cluster

    def _run_real(self, db: Session, task: Task, cluster_id: int, hub_id: int, host_id: int) -> Dict[str, Any]:
        def body(cluster: Cluster) -> Dict[str, Any]:
            from app.services.baremetal_service import baremetal_service
            hub = self.svc.get_cluster(db, hub_id)
            if hub is None:
                raise ValueError("The hub was deleted")
            host = baremetal_service.get_host(db, host_id)
            if host is None:
                raise ValueError("The bare-metal host was deleted")
            self._provision_real(db, task, hub, cluster, host)
            self.svc._set_status(db, cluster, "ready", None)
            return {"spoke": cluster.name}
        return self.svc._guarded(db, task, cluster_id, body)

    def _provision_real(self, db: Session, task: Task, hub: Cluster, cluster: Cluster, host: Any) -> None:
        from app.services.acm import conditions_state
        from app.services.baremetal_service import baremetal_service
        from app.services.openshift_addons import AddonRunner
        from app.services.openshift_service import openshift_service
        svc = self.svc
        name, version = cluster.name, cluster.version
        snet = cluster.spec["spoke_net"]

        def step(pct: int, message: str, phase: str = "preparing", progress: Optional[int] = None) -> None:
            svc._check(task, cluster, db, pct, message)
            self.installer._set_live(cluster.id, phase=phase, progress=progress, assisted_info=message)
        runner = AddonRunner(hub.name, hub.version, lambda s: svc._sleep(task, s),
                             lambda m: svc._check(task, cluster, db, None, m))

        step(5, f"Hub {hub.name}: central infrastructure management (metal3, assisted service)")
        self.ensure_cim(runner, hub, version)

        # Host DNS resolver serves api / *.apps.<spoke>.<domain> -> the node (group_id None + api_ip set).
        self.installer._reconcile_dns(db)
        # Point the real BMC's own resolver at the host DNS so it resolves the assisted-image-service route
        # (apps.<hub>.<domain>) when it fetches the discovery ISO as virtual media.
        step(12, f"Pointing {host.name}'s BMC DNS at the hub image service")
        self._ensure_bmc_dns(db, cluster, host)
        # Hub CoreDNS: resolve the spoke's zone via the host resolver, so the hub's import-controller can reach
        # the spoke API (api.<spoke>) to install the klusterlet (the hub's on-node dnsmasq only knows its own zone).
        if settings.HOST_DNS_ENABLED and not hub.group_id:
            from app.services.dns_service import dns_service
            hub_host_ip = dns_service.host_ip()
            if hub_host_ip:
                self._ensure_hub_spoke_dns(runner, f"{name}.{cluster.domain}", hub_host_ip)

        step(20, "Hub: ClusterDeployment, AgentClusterInstall, InfraEnv, NMStateConfig, BareMetalHost")
        secrets, manifests = self._spoke_manifests(db, hub, cluster, host)
        gitops = bool((cluster.spec or {}).get("gitops"))
        # Namespace + the two Secrets are app-managed prerequisites either way: the pull secret and the BMC
        # password are never committed to git. Argo CD (gitops) or a direct apply brings up the rest.
        runner.apply_retry([manifests[0]], "Namespace")
        for secret in secrets:
            runner.oc(["delete", "secret", secret["metadata"]["name"], "-n", name, "--ignore-not-found"], 30)
            out = runner.oc(["create", "-f", "-"], 60, stdin=json.dumps(secret))
            if out["exitcode"] != 0:
                raise RuntimeError(f"Secret {secret['metadata']['name']}: {(out['stderr'] or '').strip()[-300:]}")
        if gitops:
            from app.services.gitops_service import gitops_service
            step(22, "GitOps: committing the manifests to git; Argo CD on the hub syncs them")
            gitops_service.sync_spoke(db, cluster)       # write + push the manifests to the in-lab git server
            gitops_service.deploy_application(db, hub)    # ensure the Argo CD Application syncs clusters/
            self._await_gitops_sync(runner, cluster, step)  # wait for Argo to apply the CRs onto the hub
        else:
            runner.apply_retry(manifests, "spoke resources", timeout=5 * 60)
        self._monitor_real(db, task, hub, cluster, host)

    def _spoke_manifests(self, db: Session, hub: Cluster, cluster: Cluster, host: Any):
        """The real-BMH spoke's provisioning objects, in one place so the direct-apply path and the GitOps
        repo are byte-for-byte the same. Returns (secrets, manifests): the Secrets (pull secret + BMC
        credentials, applied by the app, never committed to git) and the rest (Namespace first, then the
        low-level CRs metal3 + the assisted service act on). Everything here is derived from the cluster spec
        + the host record, so gitops_service can regenerate it without running the install."""
        from app.services.baremetal_service import baremetal_service
        from app.services.openshift_service import openshift_service
        name, version = cluster.name, cluster.version
        snet = cluster.spec["spoke_net"]
        ip = snet["ip"]
        node_dns = list(snet["dns"])
        host_ip = None
        if settings.HOST_DNS_ENABLED:
            from app.services.dns_service import dns_service
            host_ip = dns_service.host_ip()
            if host_ip and host_ip not in node_dns:  # the node resolves its own + the hub's names
                node_dns = [host_ip] + node_dns
        ssh_pub = openshift_service.ssh_keypair(openshift_service.cluster_dir(name))
        creds = baremetal_service.credentials(host)
        bmc_address = baremetal_service.virtualmedia_address(db, host)
        bmc_secret = f"{host.name}-bmc"
        imageset = f"img{version}-x86-64"
        nmstate = openshift_service.nmstate_static(snet["mac"], ip, snet["prefix"], snet["gateway"], node_dns,
                                                   search=[f"{name}.{cluster.domain}"])
        secrets = [
            {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
             "metadata": {"name": "pull-secret", "namespace": name},
             "stringData": {".dockerconfigjson": openshift_service.pull_secret()}},
            {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
             "metadata": {"name": bmc_secret, "namespace": name},
             "stringData": {"username": creds["username"], "password": creds["password"]}},
        ]
        bmh_spec: Dict[str, Any] = {
            "online": True, "bootMACAddress": snet["mac"], "bootMode": "UEFI",
            "automatedCleaningMode": "metadata",  # quick metadata wipe, like the customers' ClusterInstance
            "rootDeviceHints": ({"deviceName": snet["root_device"]} if snet.get("root_device")
                                else {"minSizeGigabytes": 400}),  # robust vs guessing a disk path
            "bmc": {"address": bmc_address, "credentialsName": bmc_secret,
                    "disableCertificateVerification": True}}
        infraenv_spec: Dict[str, Any] = {
            "clusterRef": {"name": name, "namespace": name}, "pullSecretRef": {"name": "pull-secret"},
            "sshAuthorizedKey": ssh_pub, "agentLabels": {"vmm-spoke": name}, "cpuArchitecture": "x86_64",
            "nmStateConfigLabelSelector": {"matchLabels": {"infraenvs.agent-install.openshift.io": name}}}
        if snet.get("ntp"):
            infraenv_spec["additionalNTPSources"] = list(snet["ntp"])
        aci_spec: Dict[str, Any] = {
            "clusterDeploymentRef": {"name": name}, "imageSetRef": {"name": imageset},
            "networking": {"machineNetwork": [{"cidr": snet["machine_network"]}],
                           "clusterNetwork": [{"cidr": "10.128.0.0/14", "hostPrefix": 23}],
                           "serviceNetwork": ["172.30.0.0/16"], "networkType": "OVNKubernetes"},
            "provisionRequirements": {"controlPlaneAgents": 1}, "sshPublicKey": ssh_pub}
        # Groupless hub: the spoke's on-node dnsmasq only knows its own cluster zone. Install a day-0
        # MachineConfig that forwards the hub's zone to the host DNS resolver, so the spoke's klusterlet and
        # the assisted-installer-controller can reach the hub by name (api / assisted-service route).
        extra_manifests: List[Dict[str, Any]] = []
        if host_ip and not hub.group_id:
            extra_manifests.append({
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": f"{name}-manifests", "namespace": name},
                "data": {"50-vmm-hub-dns.yaml": json.dumps(
                    self._hub_dns_machineconfig(f"{hub.name}.{hub.domain}", host_ip))}})
            aci_spec["manifestsConfigMapRefs"] = [{"name": f"{name}-manifests"}]
        manifests = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}] + extra_manifests + [
            {"apiVersion": "hive.openshift.io/v1", "kind": "ClusterImageSet", "metadata": {"name": imageset},
             "spec": {"releaseImage": f"quay.io/openshift-release-dev/ocp-release:{version}-x86_64"}},
            {"apiVersion": "extensions.hive.openshift.io/v1beta1", "kind": "AgentClusterInstall",
             "metadata": {"name": name, "namespace": name}, "spec": aci_spec},
            {"apiVersion": "hive.openshift.io/v1", "kind": "ClusterDeployment",
             "metadata": {"name": name, "namespace": name},
             "spec": {"baseDomain": cluster.domain, "clusterName": name,
                      "clusterInstallRef": {"group": "extensions.hive.openshift.io", "kind": "AgentClusterInstall",
                                            "name": name, "version": "v1beta1"},
                      "platform": {"agentBareMetal": {"agentSelector": {"matchLabels": {"vmm-spoke": name}}}},
                      "pullSecretRef": {"name": "pull-secret"}}},
            {"apiVersion": "agent-install.openshift.io/v1beta1", "kind": "NMStateConfig",
             "metadata": {"name": host.name, "namespace": name,
                          "labels": {"infraenvs.agent-install.openshift.io": name}},
             "spec": {"config": nmstate,
                      "interfaces": [{"name": openshift_service.BMH_IFACE, "macAddress": snet["mac"]}]}},
            {"apiVersion": "agent-install.openshift.io/v1beta1", "kind": "InfraEnv",
             "metadata": {"name": name, "namespace": name}, "spec": infraenv_spec},
            {"apiVersion": "metal3.io/v1alpha1", "kind": "BareMetalHost",
             "metadata": {"name": host.name, "namespace": name,
                          "labels": {"infraenvs.agent-install.openshift.io": name},
                          "annotations": {"inspect.metal3.io": "disabled",
                                          "bmac.agent-install.openshift.io/hostname": cluster.nodes[0].name,
                                          "bmac.agent-install.openshift.io/role": "master"}},
             "spec": bmh_spec},
            # cloud / vendor are left unset: ACM auto-detects and sets them (so git stays clean of values a
            # controller owns, and Argo CD never churns them on a GitOps spoke).
            {"apiVersion": "cluster.open-cluster-management.io/v1", "kind": "ManagedCluster",
             "metadata": {"name": name, "labels": {"vmm-cluster-type": "spoke"}},
             "spec": {"hubAcceptsClient": True}},
            {"apiVersion": "agent.open-cluster-management.io/v1", "kind": "KlusterletAddonConfig",
             "metadata": {"name": name, "namespace": name},
             "spec": {"applicationManager": {"enabled": True}, "policyController": {"enabled": True},
                      "searchCollector": {"enabled": True}, "certPolicyController": {"enabled": True}}},
        ]
        return secrets, manifests

    def gitops_manifests(self, db: Session, cluster: Cluster) -> List[Dict[str, Any]]:
        """The non-secret provisioning manifests for a real-BMH spoke, for gitops_service to commit to git
        (the exact objects the direct-apply path applies, so Argo CD reconciles the same spoke)."""
        from app.services.baremetal_service import baremetal_service
        host_id = real_bmh_id(cluster)
        if host_id is None:
            raise ValueError("GitOps export is for spokes provisioned on a real BareMetalHost")
        host = baremetal_service.get_host(db, host_id)
        if host is None:
            raise ValueError("The spoke's bare-metal host was deleted")
        hub = self.hub_of(db, cluster)
        if hub is None:
            raise ValueError("The spoke's hub was deleted")
        _, manifests = self._spoke_manifests(db, hub, cluster, host)
        return manifests

    def _await_gitops_sync(self, runner: Any, cluster: Cluster, step: Any, timeout: int = 10 * 60) -> None:
        """Wait for Argo CD to apply the spoke's CRs onto the hub (the AgentClusterInstall appears), so the
        install then proceeds exactly like the direct-apply path (_monitor_real)."""
        deadline = time.monotonic() + timeout
        last = ""
        while True:
            aci = self._get(runner, ["get", "agentclusterinstall", cluster.name, "-n", cluster.name])
            if aci.get("metadata"):
                step(30, "Argo CD synced the spoke manifests onto the hub", "booting")
                return
            msg = "Waiting for Argo CD to sync the spoke manifests from git"
            if msg != last:
                step(25, msg, "preparing")
                last = msg
            if time.monotonic() > deadline:
                raise TimeoutError(f"Argo CD did not sync the spoke manifests within {timeout // 60} min. Check the "
                                   "Argo CD Application (hub ACM tab -> Deploy from git) and that the controller "
                                   "can reach the in-lab git server.")
            time.sleep(15)

    def _monitor_real(self, db: Session, task: Task, hub: Cluster, cluster: Cluster, host: Any) -> None:
        """Follow a real-BMH spoke's install on the hub to completion (discovery -> install -> kubeconfig ->
        ManagedCluster Available). Idempotent, so recover_interrupted can re-attach after an app restart: the
        hub keeps installing regardless, and the agents / CRs are already there."""
        from app.services.acm import conditions_state
        from app.services.openshift_addons import AddonRunner
        svc = self.svc
        name = cluster.name
        runner = AddonRunner(hub.name, hub.version, lambda s: svc._sleep(task, s),
                             lambda m: svc._check(task, cluster, db, None, m))

        def step(pct: int, message: str, phase: str = "preparing", progress: Optional[int] = None) -> None:
            svc._check(task, cluster, db, pct, message)
            self.installer._set_live(cluster.id, phase=phase, progress=progress, assisted_info=message)

        # discovery: metal3 drives the real BMC's virtual media, the agent registers
        deadline = time.monotonic() + DISCOVERY_TIMEOUT
        last = ""
        while True:
            bmh = self._get(runner, ["get", "bmh", host.name, "-n", name])
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
                raise TimeoutError(f"No agent registered after {DISCOVERY_TIMEOUT // 60} min ({msg}). Check the "
                                   f"host console (BMH page screenshot) and that {host.name}'s BMC could fetch the "
                                   "discovery ISO from the hub's assisted-image-service route.")
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
        while time.monotonic() < deadline:
            if conditions_state(self._get(runner, ["get", "managedcluster", name])).get("available"):
                break
            svc._sleep(task, 20)
        host.status = "provisioned"
        db.commit()
        self.installer._set_live(cluster.id, phase="ready")

    def resume_real(self, db: Session, cluster: Cluster) -> Optional[Task]:
        """Re-attach to a real-BMH spoke the hub is still installing (after an app restart). The CRs are on the
        hub; a new task follows the install to completion via _monitor_real."""
        from app.schemas import TaskCreate
        from app.services.task_service import task_service
        hub = self.hub_of(db, cluster)
        if hub is None or self.svc.to_dict(db, hub)["status"] != "ready":
            return None

        def run(db: Session, task: Task, cluster_id: int, hub_id: int) -> Dict[str, Any]:
            def body(cluster: Cluster) -> Dict[str, Any]:
                from app.services.baremetal_service import baremetal_service
                hub = self.svc.get_cluster(db, hub_id)
                host = baremetal_service.get_host(db, real_bmh_id(cluster))
                if hub is None or host is None:
                    raise ValueError("The hub or the bare-metal host was deleted")
                self._monitor_real(db, task, hub, cluster, host)
                self.svc._set_status(db, cluster, "ready", None)
                return {"spoke": cluster.name}
            return self.svc._guarded(db, task, cluster_id, body)
        task = task_service.start(db, TaskCreate(
            name=f"Resume spoke {cluster.name}", type="cluster_resume", target_type="cluster",
            target_id=cluster.id, target_name=cluster.name, description="Follow the ACM spoke install"),
            run, cluster.id, hub.id)
        cluster.task_id = task.id
        db.commit()
        return task

    def _ensure_bmc_dns(self, db: Session, cluster: Cluster, host: Any) -> None:
        """Make the real BMC resolve the hub's apps domain (for the virtual-media ISO fetch): set its DNS to
        the host resolver, remembering the originals so delete can restore them. Best effort."""
        from app.services.baremetal_service import baremetal_service
        if not settings.HOST_DNS_ENABLED:
            return
        from app.services.dns_service import dns_service
        host_ip = dns_service.host_ip()
        if not host_ip:
            return
        try:
            bmc = baremetal_service.client(db, host)
            current = bmc.get_dns_servers()
            if host_ip in current:
                return
            self.installer._update_spec(db, cluster, bmc_dns_orig=current)
            bmc.set_dns_servers([host_ip] + [s for s in current if s != host_ip][:1])
        except Exception as e:  # don't block the install: the fetch may still work (routable name server)
            logger.warning(f"Spoke {cluster.name}: could not point {host.name}'s BMC DNS at the host resolver "
                           f"(the ISO fetch may fail if it can't resolve the image service): {e}")

    @staticmethod
    def _hub_dns_machineconfig(zone: str, host_ip: str) -> Dict[str, Any]:
        """A day-0 MachineConfig (applied during install via AgentClusterInstall.manifestsConfigMapRefs) that
        drops /etc/dnsmasq.d/vmm-hub-forward.conf on the spoke node: forward the hub's zone to the host DNS
        resolver. The spoke's own on-node dnsmasq answers its cluster zone and otherwise returns NXDOMAIN for
        the hub's names, which shadows the resolver in resolv.conf — this explicit forward fixes it."""
        content = f"server=/{zone}/{host_ip}\n"
        data_url = "data:text/plain;base64," + base64.b64encode(content.encode()).decode()
        return {"apiVersion": "machineconfiguration.openshift.io/v1", "kind": "MachineConfig",
                "metadata": {"name": "50-vmm-hub-dns-forward",
                             "labels": {"machineconfiguration.openshift.io/role": "master"}},
                "spec": {"config": {"ignition": {"version": "3.2.0"},
                                    "storage": {"files": [{"path": "/etc/dnsmasq.d/vmm-hub-forward.conf",
                                                           "mode": 420, "overwrite": True,
                                                           "contents": {"source": data_url}}]}}}}

    def _ensure_hub_spoke_dns(self, runner: Any, spoke_zone: str, host_ip: str) -> None:
        """Add a forward on the hub's CoreDNS (dns.operator/default) for the spoke's zone -> host resolver
        (read-modify-write, keeping other spokes' entries). Non-fatal."""
        name = "vmm-spoke-" + spoke_zone.replace(".", "-")
        current = (self._get(runner, ["get", "dns.operator/default", "-o", "json"]).get("spec") or {}).get("servers") or []
        servers = [s for s in current if s.get("name") != name]
        servers.append({"name": name, "zones": [spoke_zone], "forwardPlugin": {"upstreams": [host_ip]}})
        out = runner.oc(["patch", "dns.operator/default", "--type=merge", "-p",
                         json.dumps({"spec": {"servers": servers}})], 60)
        if out["exitcode"] != 0:
            logger.warning(f"Hub DNS forward for {spoke_zone}: {(out['stderr'] or '').strip()[-200:]}")

    def _remove_hub_spoke_dns(self, runner: Any, spoke_zone: str) -> None:
        name = "vmm-spoke-" + spoke_zone.replace(".", "-")
        current = (self._get(runner, ["get", "dns.operator/default", "-o", "json"]).get("spec") or {}).get("servers") or []
        servers = [s for s in current if s.get("name") != name]
        if len(servers) != len(current):
            try:
                runner.oc(["patch", "dns.operator/default", "--type=merge", "-p",
                           json.dumps({"spec": {"servers": servers}})], 60)
            except (RuntimeError, ValueError) as e:
                logger.warning(f"Removing hub DNS forward for {spoke_zone}: {e}")

    def _restore_bmc_dns(self, db: Session, cluster: Cluster, host: Any) -> None:
        orig = (cluster.spec or {}).get("bmc_dns_orig")
        if not orig:
            return
        from app.services.baremetal_service import baremetal_service
        try:
            baremetal_service.client(db, host).set_dns_servers(orig)
        except Exception as e:
            logger.warning(f"Spoke {cluster.name}: could not restore {host.name}'s BMC DNS: {e}")

    # ---------------------------------------------------------------------- GitOps (SiteConfig)

    def siteconfig_manifest(self, db: Session, cluster: Cluster) -> str:
        """Render a real-BMH spoke as an ACM SiteConfig `ClusterInstance` (+ the BMC Secret stub), the GitOps
        artifact a customer commits for Argo CD / the SiteConfig operator to expand into the low-level CRs. The
        app applies the low-level CRs directly (this hub has only the CRD, not the operator/templates), but this
        is the same spoke expressed the way their fleet is defined. The BMC password is NOT embedded (the Secret
        stub carries the username and a placeholder; fill it in git / sealed-secrets)."""
        from app.services.baremetal_service import baremetal_service
        from app.services.openshift_service import openshift_service
        host_id = real_bmh_id(cluster)
        if host_id is None:
            raise ValueError("SiteConfig export is for spokes provisioned on a real BareMetalHost")
        host = baremetal_service.get_host(db, host_id)
        if host is None:
            raise ValueError("The spoke's bare-metal host was deleted")
        snet = (cluster.spec or {}).get("spoke_net") or {}
        name = cluster.name
        ssh_pub = openshift_service.ssh_keypair(openshift_service.cluster_dir(name))
        node_name = cluster.nodes[0].name if cluster.nodes else f"{name}-ctlplane-0"
        return self._render_siteconfig(db, host, name, cluster.domain, cluster.version, snet, node_name, ssh_pub)

    def siteconfig_preview(self, db: Session, hub: Cluster, host_id: int, net: Dict[str, Any],
                           name: Optional[str] = None, version: Optional[str] = None) -> str:
        """The SiteConfig `ClusterInstance` that WOULD be generated for a spoke on `host_id` with `net` params —
        a reference view for the spoke-create form, rendered from the real host + the parameters the user is
        typing (not a static blob), without creating anything. Missing fields are shown as placeholders; the SSH
        key is a placeholder (the real per-cluster key is made at provision time)."""
        import ipaddress
        from app.services.baremetal_service import baremetal_service
        host = baremetal_service.get_host(db, host_id)
        if host is None:
            raise ValueError(f"Bare-metal host {host_id} not found")
        name = (name or "").strip() or "spoke1"
        version = version or hub.version
        ip = (net.get("ip") or "").strip()
        gateway = (net.get("gateway") or "").strip()
        prefix = int(net.get("prefix") or 24)
        dns = [d.strip() for d in (net.get("dns") or []) if d.strip()]
        try:
            machine_net = str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False)) if ip else f"<node network>/{prefix}"
        except ValueError:
            machine_net = f"<node network>/{prefix}"
        snet = {"mac": host.boot_mac or "<host boot MAC: set it on the Bare-metal hosts page>",
                "ip": ip or "<node IP>", "prefix": prefix, "gateway": gateway or "<gateway>",
                "dns": dns or ["<dns>"], "machine_network": machine_net,
                "root_device": net.get("root_device"),
                "ntp": list(net.get("ntp") or []) or self.installer._host_ntp_servers()}
        node_name = f"{name}-ctlplane-0"
        ssh_pub = "ssh-ed25519 AAAA...  # the per-cluster key is generated when the spoke is provisioned"
        return self._render_siteconfig(db, host, name, hub.domain, version, snet, node_name, ssh_pub, preview=True)

    def _render_siteconfig(self, db: Session, host: Any, name: str, domain: str, version: str,
                           snet: Dict[str, Any], node_name: str, ssh_pub: str, preview: bool = False) -> str:
        """Build the SiteConfig `ClusterInstance` (+ BMC Secret stub) YAML for a real-BMH spoke, from the host
        record + the static-network params. One place for both the live export (siteconfig_manifest) and the
        create-form reference view (siteconfig_preview)."""
        import yaml
        from app.services.baremetal_service import baremetal_service
        from app.services.openshift_service import openshift_service
        nmstate = openshift_service.nmstate_static(snet["mac"], snet["ip"], snet["prefix"], snet["gateway"],
                                                   list(snet.get("dns") or []),
                                                   search=[f"{name}.{domain}"])
        bmc_secret = f"{host.name}-bmc"
        try:
            bmc_address = baremetal_service.virtualmedia_address(db, host)
        except Exception:
            bmc_address = f"idrac-virtualmedia://<bmc>/redfish/v1/Systems/System.Embedded.1"
        cluster_instance = {
            "apiVersion": "siteconfig.open-cluster-management.io/v1alpha1", "kind": "ClusterInstance",
            "metadata": {"name": name, "namespace": name,
                         "annotations": {"siteconfig.open-cluster-management.io/sync-wave": "1"}},
            "spec": {
                "clusterName": name, "baseDomain": domain,
                "clusterImageSetNameRef": f"img{version}-x86-64",
                "pullSecretRef": {"name": "pull-secret"}, "sshPublicKey": ssh_pub,
                "holdInstallation": False,
                "templateRefs": [{"name": "ai-cluster-templates-v1", "namespace": "open-cluster-management"}],
                "additionalNTPSources": list(snet.get("ntp") or []),
                "networkType": "OVNKubernetes",
                "machineNetwork": [{"cidr": snet.get("machine_network")}],
                "clusterNetwork": [{"cidr": "10.128.0.0/14", "hostPrefix": 23}],
                "serviceNetwork": [{"cidr": "172.30.0.0/16"}],
                "installConfigOverrides": json.dumps(
                    {"capabilities": {"baselineCapabilitySet": "None",
                                      "additionalEnabledCapabilities": ["OperatorLifecycleManager", "Ingress"]}}),
                "nodes": [{
                    "hostName": node_name,
                    "role": "master",
                    "bmcAddress": bmc_address,
                    "bmcCredentialsName": {"name": bmc_secret},
                    "bootMACAddress": snet.get("mac"), "bootMode": "UEFI",
                    "automatedCleaningMode": "metadata",
                    "rootDeviceHints": ({"deviceName": snet["root_device"]} if snet.get("root_device")
                                        else {"minSizeGigabytes": 400}),
                    "templateRefs": [{"name": "ai-node-templates-v1", "namespace": "open-cluster-management"}],
                    "nodeNetwork": {
                        "interfaces": [{"name": openshift_service.BMH_IFACE, "macAddress": snet.get("mac")}],
                        "config": nmstate},
                }],
            },
        }
        bmc_stub = {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": bmc_secret, "namespace": name},
            "stringData": {"username": host.bmc_username, "password": "<BMC_PASSWORD>"}}
        header = (("# REFERENCE — the ACM SiteConfig ClusterInstance that would define spoke "
                   f"'{name}' on host '{host.name}'.\n"
                   "# This is how a real ZTP fleet describes a cluster; the SSH key / some fields are filled in\n"
                   "# when you provision. The vm-manager app applies the expanded low-level CRs directly.\n"
                   if preview else
                   "# ACM SiteConfig ClusterInstance for spoke "
                   f"'{name}' (commit to git for Argo CD / the SiteConfig operator).\n"
                   "# The vm-manager app applies the expanded CRs directly; this is the GitOps form.\n"
                   "# Create the pull-secret Secret and fill the BMC password before applying.\n"))
        return header + "---\n".join(yaml.safe_dump(doc, sort_keys=False) for doc in (cluster_instance, bmc_stub))

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
            machine = p.get("host_name") or p.get("machine")  # real BMH name, else emulated member
            entry = {"id": c.id, "name": c.name, "machine": machine, "version": c.version,
                     "real_bmh": p.get("host_id") is not None,
                     "state": d["status"], "message": d["status_message"],
                     "ip": c.nodes[0].ip if c.nodes else None, "console_url": d.get("console_url")}
            if runner is not None:
                bmh = self._get(runner, ["get", "bmh", machine or "-", "-n", c.name])
                entry["bmh_state"] = ((bmh.get("status") or {}).get("provisioning") or {}).get("state")
                aci = self._get(runner, ["get", "agentclusterinstall", c.name, "-n", c.name])
                entry["install_state"] = ((aci.get("status") or {}).get("debugInfo") or {}).get("state")
                entry["progress"] = ((aci.get("status") or {}).get("progress") or {}).get("totalPercentage")
            out.append(entry)
        return out

    # ------------------------------------------------------------------------------------------------ delete

    def release(self, db: Session, cluster: Cluster) -> None:
        """cluster_service.delete_cluster of a spoke: its objects on the hub (no wait: the hub finishes the
        cleanup), then free the machine. Emulated: the group's VM stays (powered off, metadata cleared).
        Real BareMetalHost: eject media + power off + release it (NOT wiped) and restore its BMC DNS."""
        from app.services.baremetal_service import baremetal_service
        from app.services.cluster_service import METADATA_KEY, METADATA_URI
        hub = self.hub_of(db, cluster)
        p = provisioner(cluster) or {}
        host = baremetal_service.get_host(db, real_bmh_id(cluster)) if real_bmh_id(cluster) else None
        bmh_name = (host.name if host else None) or p.get("machine") or "-"
        if hub is not None and self.svc.to_dict(db, hub)["status"] == "ready":
            runner = self.acm._runner(hub)
            deletes = [["delete", "managedcluster", cluster.name],
                       ["delete", "clusterdeployment", cluster.name, "-n", cluster.name],
                       ["delete", "bmh", bmh_name, "-n", cluster.name]]
            if host is not None:
                deletes.append(["delete", "nmstateconfig", bmh_name, "-n", cluster.name])
            deletes.append(["delete", "namespace", cluster.name])
            for args in deletes:
                try:
                    runner.oc(args + ["--ignore-not-found", "--wait=false"], 60)
                except (RuntimeError, ValueError) as e:
                    logger.warning(f"Spoke {cluster.name}: oc {' '.join(args[:2])} on {hub.name}: {e}")
            if host is not None:  # drop the hub CoreDNS forward for this spoke's zone
                self._remove_hub_spoke_dns(runner, f"{cluster.name}.{cluster.domain}")
        elif hub is not None:
            logger.warning(f"Spoke {cluster.name}: hub {hub.name} not ready, its objects stay on the hub")
        if host is not None:  # real server: free it (power/eject only, not wiped), restore its BMC DNS
            try:
                bmc = baremetal_service.client(db, host)
                bmc.eject_media()
                bmc.clear_boot_override()
                bmc.power_off()
            except Exception as e:
                logger.warning(f"Spoke {cluster.name}: could not reset the BMC of {host.name}: {e}")
            self._restore_bmc_dns(db, cluster, host)
            host.cluster_name = None
            host.status = "available"
            host.status_message = None
            db.commit()
            from app.services.gitops_service import gitops_service
            gitops_service.remove_spoke(cluster.name)
            return
        for node in cluster.nodes:
            try:
                libvirt_client.stop_vm(node.name, force=True)
            except Exception:
                pass
            try:
                libvirt_client.set_domain_metadata(node.name, None, METADATA_KEY, METADATA_URI)
            except Exception as e:
                logger.warning(f"Spoke {cluster.name}: metadata of {node.name}: {e}")

    # ------------------------------------------------------------------------------------------------ power

    def power_real_host(self, db: Session, cluster: Cluster, on: bool) -> None:
        """Start / stop a real-BMH spoke: power its physical host on or off through the BMC."""
        from app.services.baremetal_service import baremetal_service
        host = baremetal_service.get_host(db, real_bmh_id(cluster))
        if host is None:
            raise RuntimeError("The bare-metal host was deleted")
        bmc = baremetal_service.client(db, host)
        bmc.power_on() if on else bmc.power_off(graceful=True)

    def start_real(self, db: Session, task: Task, cluster: Cluster) -> Dict[str, Any]:
        from app.services.openshift_service import openshift_service
        svc = self.svc
        svc._set_status(db, cluster, "starting", "Powering on the spoke's host")
        self.power_real_host(db, cluster, on=True)
        deadline = time.monotonic() + 30 * 60
        while True:
            openshift_service.approve_csrs(cluster.name, cluster.version)
            cv = self.installer.clusterversion(cluster)
            if cv is not None and cv.get("available"):
                svc._set_status(db, cluster, "ready", None)
                return {"nodes_ready": 1}
            if time.monotonic() > deadline:
                raise TimeoutError("The spoke API was not reachable 30 min after power on")
            svc._check(task, cluster, db, 50, "Waiting for the spoke to come back")
            svc._sleep(task, 10)

    def stop_real(self, db: Session, task: Task, cluster: Cluster) -> Dict[str, Any]:
        svc = self.svc
        svc._set_status(db, cluster, "stopping", "Powering off the spoke's host")
        self.power_real_host(db, cluster, on=False)
        svc._set_status(db, cluster, "stopped", None)
        return {"stopped": 1}


spoke_service = SpokeService()
