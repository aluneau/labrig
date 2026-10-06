"""OpenShift add-ons: OLM operators and the app-configured ones (LVMS, ODF, SR-IOV, MetalLB)

Recipes follow the product docs and kcli's `apps/<operator>/` (Namespace + OperatorGroup +
Subscription, wait for the CSV, then the operator's CR). Everything goes through `oc` on the host
(openshift_service.oc), with the cluster's host kubeconfig.
"""
import ipaddress
import json
import time
from typing import Any, Callable, Dict, List, Optional

from app.services.openshift_service import openshift_service

CSV_TIMEOUT = 15 * 60
CRD_TIMEOUT = 5 * 60
STORAGE_SERIAL = "vmm-storage"        # libvirt <serial> of the extra disk: /dev/disk/by-id/virtio-vmm-storage
STORAGE_DEVICE = f"/dev/disk/by-id/virtio-{STORAGE_SERIAL}"
DEMO_NAMESPACE = "metallb-demo"
DEMO_NAME = "hello"
SRIOV_NAMESPACE = "openshift-sriov-network-operator"


class AddonRunner:
    """Runs add-ons on one cluster. sleep(s) must raise when the task is cancelled; log(msg) reports."""

    def __init__(self, cluster_name: str, version: str, sleep: Callable[[float], None],
                 log: Callable[[str], None]):
        self.cluster = cluster_name
        self.version = version
        self.sleep = sleep
        self.log = log

    # ------------------------------------------------------------- helpers

    def oc(self, args: List[str], timeout: float = 60, stdin: Optional[str] = None) -> Dict[str, Any]:
        return openshift_service.oc(self.cluster, self.version, args, timeout, stdin)

    def oc_json(self, args: List[str], timeout: float = 60) -> Any:
        return openshift_service.oc_json(self.cluster, self.version, args, timeout)

    def apply(self, manifests: List[Dict[str, Any]]) -> None:
        openshift_service.apply(self.cluster, self.version, manifests)

    def apply_retry(self, manifests: List[Dict[str, Any]], what: str, timeout: float = CRD_TIMEOUT) -> None:
        """Apply, retrying while the CRD / webhook isn't served yet"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.apply(manifests)
                return
            except RuntimeError as e:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{what}: {e}")
                self.sleep(10)

    def package(self, name: str) -> Dict[str, Any]:
        """packagemanifest -> {source, channel, namespace, all_namespaces_only, csv}"""
        deadline = time.monotonic() + 10 * 60  # catalogs take a few minutes after install
        while True:
            try:
                pm = self.oc_json(["get", "packagemanifest", "-n", "openshift-marketplace", name], 60)
                break
            except RuntimeError as e:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Operator package {name} not found in the catalogs: {e}")
                self.sleep(15)
        status = pm.get("status") or {}
        default = status.get("defaultChannel")
        channel = next((c for c in status.get("channels") or [] if c.get("name") == default), None) or {}
        desc = channel.get("currentCSVDesc") or {}
        modes = {m.get("type"): m.get("supported") for m in desc.get("installModes") or []}
        return {
            "source": status.get("catalogSource") or "redhat-operators",
            "channel": default,
            "channels": [c.get("name") for c in status.get("channels") or []],
            "namespace": (desc.get("annotations") or {}).get("operatorframework.io/suggested-namespace"),
            "all_namespaces_only": not modes.get("OwnNamespace") and bool(modes.get("AllNamespaces")),
        }

    # ------------------------------------------------------------ operators

    def install_operator(self, name: str, channel: Optional[str] = None, source: Optional[str] = None,
                         namespace: Optional[str] = None, env: Optional[List[Dict[str, str]]] = None,
                         privileged: bool = False) -> str:
        """Namespace + OperatorGroup + Subscription, then wait for the CSV. Returns the namespace."""
        self.log(f"Installing operator {name}")
        pkg = self.package(name)
        namespace = namespace or pkg["namespace"] or f"openshift-{name.replace('-operator', '')}"
        channel = channel or pkg["channel"]
        if channel and pkg["channels"] and channel not in pkg["channels"]:
            raise ValueError(f"{name} has no channel '{channel}' (available: {', '.join(pkg['channels'])})")
        source = source if source and source != "redhat-operators" else pkg["source"]
        manifests: List[Dict[str, Any]] = []
        if namespace != "openshift-operators":
            labels = {"openshift.io/cluster-monitoring": "true"}
            if privileged:
                labels.update({"pod-security.kubernetes.io/enforce": "privileged",
                               "security.openshift.io/scc.podSecurityLabelSync": "false"})
            manifests.append({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": labels}})
            existing = self.oc(["get", "operatorgroup", "-n", namespace, "-o", "name"], 30)
            if not existing["stdout"].strip():
                og: Dict[str, Any] = {"apiVersion": "operators.coreos.com/v1", "kind": "OperatorGroup",
                                      "metadata": {"name": f"{namespace}-og", "namespace": namespace}}
                if not pkg["all_namespaces_only"]:
                    og["spec"] = {"targetNamespaces": [namespace]}
                manifests.append(og)
        sub: Dict[str, Any] = {
            "apiVersion": "operators.coreos.com/v1alpha1", "kind": "Subscription",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"name": name, "source": source, "sourceNamespace": "openshift-marketplace",
                     "installPlanApproval": "Automatic"},
        }
        if channel:
            sub["spec"]["channel"] = channel
        if env:
            sub["spec"]["config"] = {"env": env}
        manifests.append(sub)
        self.apply(manifests)
        self.wait_csv(name, namespace)
        return namespace

    def wait_csv(self, name: str, namespace: str) -> None:
        deadline = time.monotonic() + CSV_TIMEOUT
        last = "no CSV yet"
        while True:
            try:
                sub = self.oc_json(["get", "subscription", name, "-n", namespace], 30)
                csv = (sub.get("status") or {}).get("installedCSV") or (sub.get("status") or {}).get("currentCSV")
                if csv:
                    phase = self.oc(["get", "csv", csv, "-n", namespace, "-o", "jsonpath={.status.phase}"], 30)["stdout"].strip()
                    last = f"{csv}: {phase or 'pending'}"
                    if phase == "Succeeded":
                        return
                    if phase == "Failed":
                        msg = self.oc(["get", "csv", csv, "-n", namespace, "-o", "jsonpath={.status.message}"], 30)["stdout"]
                        raise RuntimeError(f"Operator {name} failed to install: {msg.strip()[-300:]}")
                else:
                    conds = [c for c in (sub.get("status") or {}).get("conditions") or [] if c.get("status") == "True"]
                    if conds:
                        last = "; ".join(f"{c.get('type')}: {c.get('message', '')}" for c in conds)[-300:]
            except RuntimeError as e:
                if "failed to install" in str(e):
                    raise
                last = str(e)[-300:]
            if time.monotonic() > deadline:
                raise TimeoutError(f"Operator {name} not installed after {CSV_TIMEOUT // 60} min ({last})")
            self.sleep(10)

    def wait_storageclass(self, name: str, timeout: float = 15 * 60) -> None:
        deadline = time.monotonic() + timeout
        while self.oc(["get", "storageclass", name], 30)["exitcode"] != 0:
            if time.monotonic() > deadline:
                raise TimeoutError(f"StorageClass {name} did not appear after {timeout // 60:.0f} min")
            self.sleep(15)

    def set_default_storageclass(self, name: str) -> None:
        self.oc(["patch", "storageclass", name, "--type", "merge", "-p",
                 json.dumps({"metadata": {"annotations": {"storageclass.kubernetes.io/is-default-class": "true"}}})], 30)

    def operator_cr(self, name: str, namespace: str) -> None:
        """CRs for catalog operators that are useless without one"""
        crs = {
            "kubernetes-nmstate-operator": {"apiVersion": "nmstate.io/v1", "kind": "NMState",
                                            "metadata": {"name": "nmstate"}},
            "kubevirt-hyperconverged": {"apiVersion": "hco.kubevirt.io/v1beta1", "kind": "HyperConverged",
                                        "metadata": {"name": "kubevirt-hyperconverged", "namespace": namespace}},
            "nfd": {"apiVersion": "nfd.openshift.io/v1", "kind": "NodeFeatureDiscovery",
                    "metadata": {"name": "nfd-instance", "namespace": namespace}, "spec": {}},
        }
        if name in crs:
            self.log(f"Creating the {crs[name]['kind']} of {name}")
            self.apply_retry([crs[name]], f"{crs[name]['kind']} for {name}")

    # -------------------------------------------------------------- storage

    def lvms(self) -> None:
        ns = self.install_operator("lvms-operator", privileged=True)
        self.log("Creating the LVMCluster (extra disk of every node)")
        self.apply_retry([{
            "apiVersion": "lvm.topolvm.io/v1alpha1", "kind": "LVMCluster",
            "metadata": {"name": "lvmcluster", "namespace": ns},
            "spec": {"storage": {"deviceClasses": [{
                "name": "vg1", "default": True, "fstype": "xfs",
                "deviceSelector": {"paths": [STORAGE_DEVICE], "forceWipeDevicesAndDestroyAllData": True},
                "thinPoolConfig": {"name": "thin-pool-1", "sizePercent": 90, "overprovisionRatio": 10},
            }]}},
        }], "LVMCluster")
        self.wait_storageclass("lvms-vg1")
        self.set_default_storageclass("lvms-vg1")

    @staticmethod
    def _odf_resources(memory: str, limit: str, cpu: str = "250m") -> Dict[str, Any]:
        # no CPU limit (no throttling); the memory limit also sizes Ceph's caches (osd_memory_target = 80 %)
        return {"requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": limit}}

    def odf(self, nodes: List[str], profile: str = "lab") -> None:
        self.install_operator("local-storage-operator", namespace="openshift-local-storage")
        for node in nodes:
            self.oc(["label", "node", node, "cluster.ocs.openshift.io/openshift-storage=", "--overwrite"], 30)
        self.log("Creating the LocalVolumeSet (extra disk of every storage node)")
        self.apply_retry([{
            "apiVersion": "local.storage.openshift.io/v1alpha1", "kind": "LocalVolumeSet",
            "metadata": {"name": "localblock", "namespace": "openshift-local-storage"},
            "spec": {
                "nodeSelector": {"nodeSelectorTerms": [{"matchExpressions": [
                    {"key": "cluster.ocs.openshift.io/openshift-storage", "operator": "Exists"}]}]},
                "storageClassName": "localblock", "volumeMode": "Block", "maxDeviceCount": 1,
                "deviceInclusionSpec": {"deviceTypes": ["disk"], "minSize": "10Gi"},
            },
        }], "LocalVolumeSet")
        self.wait_storageclass("localblock")
        ns = self.install_operator("odf-operator", namespace="openshift-storage")
        # odf-operator 4.18+ installs its dependencies; the console plugin is optional
        device_set: Dict[str, Any] = {
            "name": "ocs-deviceset-localblock", "count": 1, "replica": 3, "portable": False,
            "dataPVCTemplate": {"spec": {"accessModes": ["ReadWriteOnce"], "volumeMode": "Block",
                                         "storageClassName": "localblock",
                                         "resources": {"requests": {"storage": "1"}}}},
        }
        spec: Dict[str, Any] = {"resourceProfile": "lean", "monDataDirHostPath": "/var/lib/rook",
                                "storageDeviceSets": [device_set]}
        if profile == "lab":
            # Lab footprint: block (RBD) + file (CephFS) only, small Ceph daemons
            device_set["resources"] = self._odf_resources("2Gi", "3Gi", "500m")
            spec["resources"] = {
                "mon": self._odf_resources("1Gi", "2Gi"),
                "mgr": self._odf_resources("512Mi", "1536Mi"),
                "mds": self._odf_resources("1Gi", "2Gi"),
            }
            spec["multiCloudGateway"] = {"reconcileStrategy": "ignore"}   # no NooBaa (core + Postgres + endpoint)
            spec["managedResources"] = {"cephObjectStores": {"reconcileStrategy": "ignore"},   # no RGW
                                        "cephObjectStoreUsers": {"reconcileStrategy": "ignore"}}
        self.log(f"Creating the StorageCluster (Ceph, {'lab footprint: no object storage' if profile == 'lab' else 'resource profile lean'})")
        self.apply_retry([{
            "apiVersion": "ocs.openshift.io/v1", "kind": "StorageCluster",
            "metadata": {"name": "ocs-storagecluster", "namespace": ns}, "spec": spec,
        }], "StorageCluster", timeout=10 * 60)
        self.log("Waiting for Ceph (ocs-storagecluster-ceph-rbd)")
        self.wait_storageclass("ocs-storagecluster-ceph-rbd", timeout=30 * 60)
        self.set_default_storageclass("ocs-storagecluster-ceph-rbd")

    # --------------------------------------------------------------- SR-IOV

    @staticmethod
    def sriov_machineconfigs(roles: List[str]) -> List[Dict[str, Any]]:
        """Day-1 manifests (openshift/ dir): IOMMU on for vfio-pci VFs"""
        return [{
            "apiVersion": "machineconfiguration.openshift.io/v1", "kind": "MachineConfig",
            "metadata": {"name": f"99-{role}-iommu", "labels": {"machineconfiguration.openshift.io/role": role}},
            "spec": {"kernelArguments": ["intel_iommu=on", "iommu=pt"]},
        } for role in roles]

    def sriov(self, options: Dict[str, Any], single_or_compact: bool) -> None:
        # igb (82576) isn't in the operator's supported-nic-ids: dev mode lifts the webhook and
        # the config daemon's check
        self.install_operator("sriov-network-operator", namespace=SRIOV_NAMESPACE,
                              env=[{"name": "DEV_MODE", "value": "TRUE"}])
        self.log("Configuring the SR-IOV operator (dev mode, no drain on small clusters)")
        self.apply_retry([{
            "apiVersion": "sriovnetwork.openshift.io/v1", "kind": "SriovOperatorConfig",
            "metadata": {"name": "default", "namespace": SRIOV_NAMESPACE},
            "spec": {"enableInjector": True, "enableOperatorWebhook": False,
                     "disableDrain": bool(single_or_compact), "logLevel": 2},
        }], "SriovOperatorConfig")
        # The policy controller ignores nodes whose state has no interfaces yet and doesn't look again
        # when they appear: wait for the config daemons' discovery first
        self.log("Waiting for the SR-IOV config daemon to discover the igb NICs")
        deadline = time.monotonic() + 10 * 60
        while True:
            states = self.oc_json(["get", "sriovnetworknodestates", "-n", SRIOV_NAMESPACE], 30).get("items", []) \
                if self.oc(["get", "crd", "sriovnetworknodestates.sriovnetwork.openshift.io"], 30)["exitcode"] == 0 else []
            if states and all(any(i.get("deviceID") == "10c9" for i in (st.get("status") or {}).get("interfaces") or [])
                              for st in states):
                break
            if time.monotonic() > deadline:
                raise TimeoutError("The SR-IOV config daemon found no igb NIC on the nodes")
            self.sleep(10)
        device_type = options.get("device_type") or "netdevice"
        resource = "igbnetdev" if device_type == "netdevice" else "igbvfio"
        demo_ns = "sriov-demo"
        self.log(f"Creating the SR-IOV policy ({options.get('vfs', 4)} VFs per igb NIC, {device_type})")
        self.apply_retry([
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": demo_ns}},
            {"apiVersion": "sriovnetwork.openshift.io/v1", "kind": "SriovNetworkNodePolicy",
             "metadata": {"name": f"igb-{device_type}", "namespace": SRIOV_NAMESPACE},
             "spec": {"resourceName": resource, "nodeSelector": {"node-role.kubernetes.io/worker": ""},
                      "numVfs": int(options.get("vfs", 4)),
                      "nicSelector": {"vendor": "8086", "deviceID": "10c9"},
                      "deviceType": device_type}},
            {"apiVersion": "sriovnetwork.openshift.io/v1", "kind": "SriovNetwork",
             "metadata": {"name": "igb-net", "namespace": SRIOV_NAMESPACE},
             "spec": {"resourceName": resource, "networkNamespace": demo_ns,
                      "ipam": json.dumps({"type": "whereabouts", "range": options.get("ipam_range") or "192.168.50.0/24"})}},
        ], "SR-IOV policy")
        self.log("Waiting for the VFs (SriovNetworkNodeState in sync, device plugin resource allocatable)")
        deadline = time.monotonic() + 20 * 60  # vfio-pci / drains can reboot nodes
        want = int(options.get("vfs", 4))
        while True:
            states = self.oc_json(["get", "sriovnetworknodestates", "-n", SRIOV_NAMESPACE], 30).get("items", [])
            synced = [st for st in states if (st.get("status") or {}).get("syncStatus") == "Succeeded"
                      and any(i.get("numVfs") == want for i in (st.get("status") or {}).get("interfaces") or [])]
            nodes = self.oc_json(["get", "nodes"], 30).get("items", [])
            allocatable = [n for n in nodes
                           if int(((n.get("status") or {}).get("allocatable") or {}).get(f"openshift.io/{resource}", "0") or 0) > 0]
            if states and len(synced) == len(states) and allocatable:
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"VFs not ready after 20 min ({len(synced)}/{len(states)} nodes in sync, "
                                   f"{len(allocatable)} with openshift.io/{resource} allocatable)")
            self.sleep(15)

    # -------------------------------------------------------------- MetalLB

    def metallb(self, pool: str, mode: str = "l2", bgp: Optional[Dict[str, Any]] = None) -> None:
        """MetalLB CR + IPAddressPool, then L2Advertisement (l2) or BGPPeer + BGPAdvertisement (bgp:
        bgp = {router_ip, my_asn, peer_asn}). Switching modes removes the other mode's objects."""
        self.install_operator("metallb-operator", namespace="metallb-system")
        self.log(f"Configuring MetalLB ({mode.upper()}, pool {pool})")
        self.apply_retry([{"apiVersion": "metallb.io/v1beta1", "kind": "MetalLB",
                           "metadata": {"name": "metallb", "namespace": "metallb-system"}}], "MetalLB")
        manifests: List[Dict[str, Any]] = [
            {"apiVersion": "metallb.io/v1beta1", "kind": "IPAddressPool",
             "metadata": {"name": "lab-pool", "namespace": "metallb-system"},
             "spec": {"addresses": [pool], "autoAssign": True, "avoidBuggyIPs": True}},
        ]
        if mode == "bgp":
            if not bgp:
                raise ValueError("MetalLB BGP mode needs the router's BGP settings")
            manifests += [
                {"apiVersion": "metallb.io/v1beta2", "kind": "BGPPeer",
                 "metadata": {"name": "lab-router", "namespace": "metallb-system"},
                 "spec": {"myASN": int(bgp["my_asn"]), "peerASN": int(bgp["peer_asn"]),
                          "peerAddress": bgp["router_ip"], "holdTime": "30s", "keepaliveTime": "10s"}},
                {"apiVersion": "metallb.io/v1beta1", "kind": "BGPAdvertisement",
                 "metadata": {"name": "lab-bgp", "namespace": "metallb-system"},
                 "spec": {"ipAddressPools": ["lab-pool"]}},
            ]
            stale = [["l2advertisement", "lab-l2"]]
        else:
            manifests.append({"apiVersion": "metallb.io/v1beta1", "kind": "L2Advertisement",
                              "metadata": {"name": "lab-l2", "namespace": "metallb-system"},
                              "spec": {"ipAddressPools": ["lab-pool"]}})
            stale = [["bgpadvertisement", "lab-bgp"], ["bgppeers.metallb.io", "lab-router"]]
        self.apply_retry(manifests, "IPAddressPool / advertisement")
        for kind, name in stale:
            self.oc(["delete", kind, name, "-n", "metallb-system", "--ignore-not-found"], 60)

    def metallb_demo(self, timeout: float = 10 * 60, pool: Optional[str] = None, mode: str = "l2") -> str:
        """hello Deployment (2 replicas, answers with its pod and node) + LoadBalancer Service.
        Returns the service's external IP. An existing Service whose IP is outside `pool` (the pool
        changed with the mode) is re-created to get an address of the new pool."""
        self.log("Deploying the MetalLB demo (hello)")
        current = self.service_ip()
        if current and pool and not in_pool(current, pool):
            self.log(f"Re-creating the hello Service: {current} is not in the new pool {pool}")
            self.oc(["delete", "svc", DEMO_NAME, "-n", DEMO_NAMESPACE, "--ignore-not-found"], 60)
        # the image's docroot isn't writable by OpenShift's random UID: serve the page from an emptyDir
        script = (f'echo "Hello from pod $POD_NAME on node $NODE_NAME (MetalLB {mode.upper()} lab)" '
                  "> /var/www/html/index.html; exec run-httpd")
        self.apply([
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": DEMO_NAMESPACE}},
            {"apiVersion": "apps/v1", "kind": "Deployment",
             "metadata": {"name": DEMO_NAME, "namespace": DEMO_NAMESPACE, "labels": {"app": DEMO_NAME}},
             "spec": {"replicas": 2, "selector": {"matchLabels": {"app": DEMO_NAME}},
                      "template": {"metadata": {"labels": {"app": DEMO_NAME}}, "spec": {
                          "containers": [{
                              "name": "httpd", "image": "registry.access.redhat.com/ubi9/httpd-24",
                              "command": ["/bin/sh", "-c", script],
                              "ports": [{"containerPort": 8080}],
                              "env": [{"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
                                      {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}],
                              "readinessProbe": {"httpGet": {"path": "/", "port": 8080}, "periodSeconds": 5},
                              "volumeMounts": [{"name": "docroot", "mountPath": "/var/www/html"}],
                          }],
                          "volumes": [{"name": "docroot", "emptyDir": {}}]}}}},
            {"apiVersion": "v1", "kind": "Service",
             "metadata": {"name": DEMO_NAME, "namespace": DEMO_NAMESPACE,
                          "annotations": {"metallb.io/address-pool": "lab-pool"}},
             "spec": {"type": "LoadBalancer", "selector": {"app": DEMO_NAME},
                      "ports": [{"name": "http", "port": 80, "targetPort": 8080}]}},
        ])
        deadline = time.monotonic() + timeout
        while True:
            ip = self.service_ip()
            if ip:
                return ip
            if time.monotonic() > deadline:
                raise TimeoutError("The hello Service got no external IP from MetalLB")
            self.sleep(5)

    def service_ip(self) -> Optional[str]:
        out = self.oc(["get", "svc", DEMO_NAME, "-n", DEMO_NAMESPACE, "-o",
                       "jsonpath={.status.loadBalancer.ingress[0].ip}"], 30)
        ip = out["stdout"].strip() if out["exitcode"] == 0 else ""
        return ip or None

    def announcing_node(self) -> Optional[str]:
        """Node whose speaker answers ARP for the demo IP: ServiceL2Status (MetalLB >= 0.14), else the
        latest nodeAssigned event"""
        out = self.oc(["get", "servicel2statuses.metallb.io", "-n", "metallb-system", "-o", "json"], 30)
        if out["exitcode"] == 0:
            try:
                for item in json.loads(out["stdout"]).get("items", []):
                    svc = (item.get("status") or {}).get("serviceName")
                    if svc == DEMO_NAME and (item.get("status") or {}).get("serviceNamespace") == DEMO_NAMESPACE:
                        return (item.get("status") or {}).get("node")
            except ValueError:
                pass
        out = self.oc(["get", "events", "-n", DEMO_NAMESPACE, "--field-selector",
                       f"involvedObject.name={DEMO_NAME},reason=nodeAssigned", "-o", "json"], 30)
        if out["exitcode"] == 0:
            try:
                events = sorted(json.loads(out["stdout"]).get("items", []),
                                key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
                if events:
                    msg = events[-1].get("message") or ""  # 'announcing from node "x" with protocol "layer2"'
                    if 'node "' in msg:
                        return msg.split('node "', 1)[1].split('"', 1)[0]
            except ValueError:
                pass
        return None

    def demo_endpoints(self) -> List[Dict[str, Any]]:
        out = self.oc(["get", "pods", "-n", DEMO_NAMESPACE, "-l", f"app={DEMO_NAME}", "-o", "json"], 30)
        if out["exitcode"] != 0:
            return []
        result = []
        for pod in json.loads(out["stdout"]).get("items", []):
            ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in (pod.get("status") or {}).get("conditions") or [])
            result.append({"pod": pod["metadata"]["name"], "node": (pod.get("spec") or {}).get("nodeName"),
                           "ip": (pod.get("status") or {}).get("podIP"), "ready": ready})
        return result


def in_pool(ip: str, pool: str) -> bool:
    """pool: "a-b" range or a CIDR"""
    try:
        addr = ipaddress.IPv4Address(ip)
        if "/" in pool:
            return addr in ipaddress.IPv4Network(pool, strict=False)
        start, _, end = pool.partition("-")
        return ipaddress.IPv4Address(start.strip()) <= addr <= ipaddress.IPv4Address((end or start).strip())
    except ValueError:
        return False


def metallb_pool(cidr: str, dhcp_end: str, size: int, taken: List[str]) -> str:
    """Pick `size` consecutive free addresses after the DHCP range (static pool, top of the subnet)"""
    net = ipaddress.IPv4Network(cidr)
    hosts = list(net.hosts())
    end = ipaddress.IPv4Address(dhcp_end)
    used = {ipaddress.IPv4Address(ip) for ip in taken}
    run: List[ipaddress.IPv4Address] = []
    for h in hosts[:-1]:  # keep the last host address free
        if h <= end or h in used:
            run = []
            continue
        run.append(h)
        if len(run) == size:
            return f"{run[0]}-{run[-1]}"
    raise ValueError(f"No {size} free consecutive addresses after the DHCP range in {cidr} for MetalLB")
