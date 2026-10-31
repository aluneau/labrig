"""OpenShift cluster options (agent-based installer) and the /openshift API"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# 4.20.39, 4.21.0-rc.2, 5.0.0-ec.1; OKD: 5.0.0-okd-scos.3, 5.1.0-okd-scos.ec.0
OCP_VERSION = r"^\d+\.\d+\.\d+(-(rc|ec)\.\d+|-okd-scos(\.ec)?\.\d+)?$"
IPV4 = r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$"
OCP_CHANNEL = r"^(stable|fast|candidate|eus)-\d+\.\d+$"
PACKAGE = r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$"


class OperatorRequest(BaseModel):
    """An OLM operator to install (Namespace + OperatorGroup + Subscription, then its CR if known)"""
    name: str = Field(..., pattern=PACKAGE)              # package name, e.g. "kubernetes-nmstate-operator"
    channel: Optional[str] = Field(None, max_length=63)  # None = the package's defaultChannel
    source: str = Field("redhat-operators", pattern=PACKAGE)
    namespace: Optional[str] = Field(None, pattern=PACKAGE)  # None = suggested namespace / openshift-<name>


class SriovOptions(BaseModel):
    """Emulated SR-IOV on every node: vIOMMU + igb NICs (82576, up to 7 VFs), SR-IOV Network Operator
    in dev mode (igb isn't in its supported NIC list), a sample node policy + SriovNetwork."""
    enabled: bool = False
    nics: int = Field(1, ge=1, le=4)        # igb NICs per node, on the group network
    vfs: int = Field(4, ge=1, le=7)         # VFs per igb NIC in the sample policy
    device_type: Literal["netdevice", "vfio-pci"] = "netdevice"
    # whereabouts range of the sample SriovNetwork (secondary pod network, must not overlap the group)
    ipam_range: str = "192.168.50.0/24"


class MetalLBOptions(BaseModel):
    """MetalLB with an address pool assigned by the app. demo = the "MetalLB lab" scenario: a hello
    Deployment + LoadBalancer Service + DNS record hello.<domain>.
    mode "l2": the pool is carved out of the group network (kept out of DHCP and static assignments),
    one node answers ARP for each service IP.
    mode "bgp": the pool is a /27 outside the group network (BGP_ANNOUNCE_POOL, recorded as a BGP
    announce range of the group router); every node peers with the router (AS 64513 -> 64512) and
    announces the service IPs, the router spreads traffic over them (ECMP).
    bfd (bgp mode): turn BFD on on the group router (if it isn't) and give the BGPPeer a BFDProfile with the
    router's timers: a dead node's routes are withdrawn in < 1 s instead of the 30 s hold time. The profile
    follows the router's BFD setting whenever the configuration is applied (create, mode switch)."""
    enabled: bool = False
    mode: Literal["l2", "bgp"] = "l2"
    addresses: int = Field(16, ge=2, le=64)   # size of the L2 pool (BGP: always a /27)
    demo: bool = True
    bfd: bool = False
    # assigned by the app: "10.43.5.230-10.43.5.245" (l2) or "10.45.0.0/27" (bgp)
    pool: Optional[str] = None


class OpenShiftOptions(BaseModel):
    distribution: Literal["ocp", "okd"] = Field("ocp", description=(
        "ocp = OpenShift Container Platform (needs a Red Hat pull secret); okd = OKD, the community distribution "
        "on CentOS Stream CoreOS (quay.io/okd/scos-release, no pull secret). OKD: community operators only, no "
        "ODF / SR-IOV / ACM / disconnected"))
    version: Optional[str] = Field(None, pattern=OCP_VERSION)  # None = latest of the channel
    channel: str = Field("stable-4.20", pattern=OCP_CHANNEL)
    platform: Literal["none", "baremetal"] = Field("none", description=(
        "none = the group router is the external DNS + load balancer (api / *.apps -> router haproxy); baremetal = "
        "the cluster runs its own keepalived + haproxy on two virtual IPs of the group network (api_vip, "
        "ingress_vip; DNS points at them; the router only forwards its uplink :6443 / :80 / :443 to them for the "
        "host). baremetal needs 3 control planes (compact or ha): the installer refuses it for single node"))
    # baremetal: virtual IPs on the group network (kept out of DHCP); None = picked by the app
    api_vip: Optional[str] = Field(None, pattern=IPV4)
    ingress_vip: Optional[str] = Field(None, pattern=IPV4)
    topology: Literal["sno", "compact", "ha"] = Field("sno", description=(
        "sno = 1 node; compact = 3 masters that also run workloads (and ODF); ha = 3 masters + `workers` workers"))
    storage: Literal["none", "lvms", "odf"] = Field("none", description=(
        "Storage installed AND configured by the app (operator, an extra disk per storage node, StorageCluster / "
        "LVMCluster, default StorageClass). lvms = node-local, any topology; odf = OpenShift Data Foundation (Ceph), "
        "needs 3 nodes: topology compact (on the masters) or ha with 3+ workers (on the workers); node sizes get "
        "the ODF overhead added when ctlplane / worker are left out. Don't list lvms-operator / odf-operator / "
        "local-storage-operator in operators"))
    storage_disk_size: int = Field(100, ge=20, le=2048, description="GiB, extra disk per storage node (LVMS / ODF)")
    odf_profile: Literal["lab", "lean"] = Field("lab", description=(
        "ODF footprint: lab = small Ceph requests/limits, no object storage (NooBaa, RGW), +6 GiB / +2 vCPU per "
        "storage node; lean = Red Hat sizing, +24 GiB / +8 vCPU"))
    operators: List[OperatorRequest] = Field([], description=(
        "Plain OLM operators (search_operators). Storage, SR-IOV, MetalLB and ACM are options of their own "
        "(storage / sriov / metallb / acm), not operators"))
    sriov: SriovOptions = SriovOptions()
    metallb: MetalLBOptions = MetalLBOptions()
    # Red Hat Advanced Cluster Management hub (docs/openshift.md "ACM hub"): the ACM operator (it installs the
    # multicluster engine) + a MultiClusterHub (availabilityConfig Basic); other clusters of the app are imported
    # with POST /clusters/{id}/acm/managed
    acm: bool = False
    # IPv6 dual stack (docs/ipv6.md, create only): machine / cluster / service networks of both families (OVN-
    # Kubernetes), api / *.apps AAAA records, MetalLB L2 IPv6 pool. Needs a group with IPv6 (an auto-created
    # group gets it). Nodes take their IPv6 address from the group's DHCPv6 reservations. MetalLB BGP: an IPv6
    # announce range + IPv6 sessions too; platform baremetal: IPv6 twins of the API / ingress VIPs.
    dual_stack: bool = False
    # Stop offering updates (clusterversion channel cleared)
    disable_updates: bool = True
    # Disconnected install (create time only, docs/disconnected.md): the group router runs a mirror
    # registry filled by oc-mirror (release + the add-ons' operators + demo images), the group's egress is
    # blocked before the nodes boot, the cluster pulls everything from the mirror (registry-only pull secret)
    disconnected: bool = False

    @model_validator(mode="after")
    def _distribution(self) -> "OpenShiftOptions":
        if self.version and ("-okd-" in self.version) != (self.distribution == "okd"):
            raise ValueError(f"Version {self.version} is not an {self.distribution.upper()} release "
                             + ("(OKD versions look like 5.0.0-okd-scos.3)" if self.distribution == "okd"
                                else '(set "distribution": "okd" for an OKD release)'))
        if self.platform == "none" and (self.api_vip or self.ingress_vip):
            raise ValueError('api_vip / ingress_vip only apply to "platform": "baremetal"')
        if self.api_vip and self.api_vip == self.ingress_vip:
            raise ValueError("api_vip and ingress_vip must differ")
        return self

    @field_validator("operators")
    @classmethod
    def _unique(cls, v: List[OperatorRequest]) -> List[OperatorRequest]:
        names = [o.name for o in v]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"Operator listed twice: {', '.join(sorted(dupes))}")
        return v


class PullSecretIn(BaseModel):
    # The pull secret JSON (console.redhat.com/openshift/install/pull-secret), or a host path to read it from
    content: Optional[str] = None
    path: Optional[str] = None


class PullSecretStatus(BaseModel):
    configured: bool
    registries: List[str] = []   # auths keys (never the tokens)
    source: Optional[str] = None  # where it was imported from


class OpenShiftChannel(BaseModel):
    name: str            # stable-4.20
    minor: str           # 4.20
    latest: Optional[str] = None


class OpenShiftVersion(BaseModel):
    version: str
    payload: Optional[str] = None   # release image pullspec
    cached: bool = False            # openshift-install + oc already downloaded


class CatalogOperator(BaseModel):
    """Curated operator offered at install time (before the cluster exists, no live catalog)"""
    name: str
    display_name: str
    description: str
    source: str = "redhat-operators"
    category: str = "Other"
    # The app also configures it (CR, storage class, sample objects) when selected through an option
    managed_by: Optional[str] = None  # "storage:lvms" | "storage:odf" | "sriov" | "metallb" | "acm"
    min_nodes: int = 1


class InstalledOperator(BaseModel):
    name: str
    namespace: str
    channel: Optional[str] = None
    source: Optional[str] = None
    csv: Optional[str] = None
    phase: Optional[str] = None     # Succeeded, Installing, Failed...
    version: Optional[str] = None


class PackageManifest(BaseModel):
    """Live catalog entry of an installed cluster (openshift-marketplace packagemanifests)"""
    name: str
    display_name: Optional[str] = None
    provider: Optional[str] = None
    source: str
    default_channel: Optional[str] = None
    channels: List[str] = []
    description: Optional[str] = None
    suggested_namespace: Optional[str] = None
    all_namespaces_only: bool = False


class ClusterCredentials(BaseModel):
    username: str = "kubeadmin"
    password: Optional[str] = None
    console_url: Optional[str] = None


class AddonRequest(BaseModel):
    """Day-2: enable an add-on on an installed cluster"""
    kind: Literal["operator", "lvms", "odf", "sriov", "metallb", "metallb-demo", "acm", "gitops"]
    operator: Optional[OperatorRequest] = None
    sriov: Optional[SriovOptions] = None
    metallb: Optional[MetalLBOptions] = None


class ScenarioCheck(BaseModel):
    name: str
    ok: Optional[bool] = None
    detail: str = ""


class MetalLBScenario(BaseModel):
    """State of the "MetalLB lab" scenario for the diagram + checks"""
    enabled: bool
    mode: str = "l2"
    bgp_peers: List[Dict[str, Any]] = []      # bgp: [{node, ip, state, bfd}] sessions of the nodes with the router
    bfd: bool = False                         # bgp: BFD on the router (the BGPPeer gets a BFDProfile)
    state: Optional[str] = None               # kubeadm: pending | installing | done | error (last apply)
    message: Optional[str] = None
    bgp_nexthops: List[str] = []              # bgp: nodes the router routes the service IP to (ECMP)
    pool: Optional[str] = None
    service_ip: Optional[str] = None
    hostname: Optional[str] = None          # hello.<domain>
    announcing_node: Optional[str] = None   # node whose speaker answers ARP for the service IP
    endpoints: List[Dict[str, Any]] = []    # [{pod, node, ip}]
    router_ip: Optional[str] = None
    group_cidr: Optional[str] = None
    wireguard: bool = False
    wireguard_port: Optional[int] = None    # UDP port of the host relay
    checks: List[ScenarioCheck] = []


class AssistedHost(BaseModel):
    name: str
    role: Optional[str] = None
    status: Optional[str] = None
    stage: Optional[str] = None          # e.g. "Writing image to disk", "Rebooting", "Done"
    progress: Optional[int] = None       # %


class ClusterOperatorStatus(BaseModel):
    name: str
    available: Optional[bool] = None
    progressing: Optional[bool] = None
    degraded: Optional[bool] = None
    message: Optional[str] = None
    version: Optional[str] = None


class InstallStatus(BaseModel):
    """Live install / health view of an OpenShift cluster"""
    phase: str                           # preparing | booting | installing | finalizing | addons | ready | error | stopped
    day2: bool = False                   # a day-2 add-on install on a ready cluster (show add-on progress, not the stepper)
    assisted_status: Optional[str] = None
    assisted_info: Optional[str] = None
    progress: Optional[int] = None       # Assisted Service total %
    hosts: List[AssistedHost] = []
    version: Optional[str] = None
    cluster_operators: List[ClusterOperatorStatus] = []
    addons: List[Dict[str, Any]] = []    # [{kind, name, state: pending|installing|done|error, message}]


class AcmImportRequest(BaseModel):
    """Import another cluster of the app into an ACM hub"""
    cluster_id: int
    # auto: auto-import-secret when the hub reaches the cluster's API, else the app applies the import manifests
    # itself; secret / manifests: force one of them
    method: Literal["auto", "secret", "manifests"] = "auto"


class AcmManagedCluster(BaseModel):
    name: str                            # ManagedCluster name (= the app cluster's name)
    cluster_id: Optional[int] = None     # app cluster (None: imported by hand on the hub)
    cluster_type: Optional[str] = None   # kubeadm | k3s | openshift
    state: str = "unknown"               # importing | available | unavailable | detaching | error | unknown
    method: Optional[str] = None         # secret (auto-import-secret) | manifests (applied by the app)
    message: Optional[str] = None
    joined: Optional[bool] = None        # ManagedClusterJoined
    available: Optional[bool] = None     # ManagedClusterConditionAvailable (None = Unknown)
    hub_accepted: Optional[bool] = None
    kubernetes_version: Optional[str] = None
    vendor: Optional[str] = None
    conditions: List[Dict[str, Any]] = []


class AcmStatus(BaseModel):
    """ACM tab of an OpenShift cluster: hub state + managed clusters"""
    enabled: bool = False
    state: Optional[str] = None          # add-on: pending | installing | done | error
    message: Optional[str] = None
    hub_phase: Optional[str] = None      # MultiClusterHub status.phase (Running...)
    hub_version: Optional[str] = None
    mce_phase: Optional[str] = None
    console_url: Optional[str] = None    # OpenShift console, "All Clusters" perspective
    hub_api: Optional[str] = None        # what klusterlets connect to
    sizing: Optional[str] = None         # plain words when the cluster is too small for a hub
    managed: List[AcmManagedCluster] = []
    importable: List[Dict[str, Any]] = []  # [{id, name, type, status, note}]
    # Spokes the hub provisioned on BMC machines: [{name, machine, group_id, version, state, message, ip,
    # bmh_state, install_state, progress, console_url}]
    spokes: List[Dict[str, Any]] = []
    # BMC members of the hub's group not used by a spoke: [{name, memory, vcpu, redfish_url}]
    spoke_machines: List[Dict[str, Any]] = []
    # Registered REAL BareMetalHosts available as a spoke machine: [{id, name, memory_gib, cpu_count}]
    spoke_hosts: List[Dict[str, Any]] = []
    error: Optional[str] = None


# Node roles for a real bare-metal spoke. "master" = control plane, "worker" = compute, "arbiter" = the
# lightweight etcd quorum tiebreaker of a Two-Node OpenShift with Arbiter (TNA) cluster (OCP 4.20 GA).
SPOKE_ROLE = Literal["master", "arbiter", "worker"]

# Supported real-bare-metal spoke topologies, with the (controlPlaneAgents, arbiterAgents, workerAgents)
# each maps to in the AgentClusterInstall provisionRequirements. "tna" is the GA 2-node control plane
# (2 control plane + 1 arbiter = 3 physical hosts); a true 2-physical-node cluster (TNF, fencing) is Tech
# Preview in 4.20 and is not expressible through the AgentClusterInstall CR, so it is not offered here.
SPOKE_TOPOLOGY = Literal["sno", "tna", "compact", "ha"]


class AcmSpokeNet(BaseModel):
    """The real bare-metal spoke node's static network (there is no lab-group router / DHCP). For a
    multi-node spoke these are the SHARED network parameters (prefix / gateway / dns / ntp); each host's own
    IP + root device come from the `hosts` list. `ip` is used only for the single-node (host_id) shape."""
    ip: Optional[str] = None
    prefix: int = Field(..., ge=1, le=32)
    gateway: str
    dns: List[str] = []
    ntp: List[str] = []
    root_device: Optional[str] = None  # e.g. /dev/sda; empty = the installer picks the first eligible disk
    # Multi-node only: the on-cluster load balancer (keepalived) VIPs, on the machine network, distinct from
    # every node IP. SNO uses user-managed networking (the single node IS the API) and needs neither.
    api_vip: Optional[str] = None
    ingress_vip: Optional[str] = None


class AcmSpokeNode(BaseModel):
    """One physical host of a multi-node real-bare-metal spoke: a registered BareMetalHost + its static IP
    on the machine network + its role."""
    host_id: int
    ip: str
    role: SPOKE_ROLE = "master"
    root_device: Optional[str] = None  # per-host override; empty = the installer picks the first eligible disk


class AcmSpokeRequest(BaseModel):
    """The hub provisions an OpenShift spoke, on exactly one of:
      - `machine`: an emulated-BMC member of the hub's lab group (single node, SNO); or
      - `host_id` + `net`: a single REAL BareMetalHost (SNO); or
      - `hosts` + `net`: several REAL BareMetalHosts (multi-node: compact / HA / two-node-with-arbiter),
        each with its own IP + role, into ONE cluster with the right provisionRequirements."""
    name: str = Field(..., min_length=1, max_length=40, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
    machine: Optional[str] = Field(None, pattern=PACKAGE)   # emulated-BMC member of the hub's group
    host_id: Optional[int] = None                           # a single registered real BareMetalHost
    hosts: Optional[List[AcmSpokeNode]] = Field(None, min_length=1, max_length=6)  # several real hosts
    net: Optional[AcmSpokeNet] = None                       # required with host_id / hosts
    topology: Optional[SPOKE_TOPOLOGY] = None               # default inferred from the host roles / count
    version: Optional[str] = Field(None, pattern=OCP_VERSION)  # default: the hub's
    gitops: bool = False  # deploy "the realistic way": write the manifests to git and let Argo CD sync them
                          #                            onto the hub, instead of the app applying the CRs directly

    @model_validator(mode="after")
    def _one_machine(self) -> "AcmSpokeRequest":
        chosen = [k for k, v in (("machine", self.machine), ("host_id", self.host_id),
                                 ("hosts", self.hosts)) if v is not None]
        if len(chosen) != 1:
            raise ValueError("Give exactly one of 'machine' (emulated-BMC member), 'host_id' (one real "
                             "BareMetalHost) or 'hosts' (several real BareMetalHosts)")
        if (self.host_id is not None or self.hosts is not None) and self.net is None:
            raise ValueError("A real bare-metal spoke needs the node's static 'net' (prefix, gateway, dns)")
        if self.host_id is not None and not (self.net and self.net.ip):
            raise ValueError("A single-host real spoke (host_id) needs net.ip (the node's static IP)")
        if self.hosts is not None:
            ids = [h.host_id for h in self.hosts]
            if len(set(ids)) != len(ids):
                raise ValueError("The same host appears more than once in 'hosts'")
            if len(self.hosts) > 1 and not (self.net and self.net.api_vip and self.net.ingress_vip):
                raise ValueError("A multi-node spoke needs net.api_vip and net.ingress_vip (the on-cluster "
                                 "load-balancer VIPs on the machine network)")
        return self
