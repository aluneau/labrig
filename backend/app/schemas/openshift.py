"""OpenShift cluster options (agent-based installer) and the /openshift API"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# 4.20.39, 4.21.0-rc.2, 5.0.0-ec.1
OCP_VERSION = r"^\d+\.\d+\.\d+(-(rc|ec)\.\d+)?$"
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
    """MetalLB in L2 mode with an address pool carved out of the group network (kept out of DHCP and
    static assignments). demo = the "MetalLB L2 lab" scenario: a hello Deployment + LoadBalancer
    Service + DNS record hello.<domain>."""
    enabled: bool = False
    addresses: int = Field(16, ge=2, le=64)   # size of the pool (assigned by the app)
    demo: bool = True
    # assigned by the app: "10.43.5.230-10.43.5.245"
    pool: Optional[str] = None


class OpenShiftOptions(BaseModel):
    version: Optional[str] = Field(None, pattern=OCP_VERSION)  # None = latest of the channel
    channel: str = Field("stable-4.20", pattern=OCP_CHANNEL)
    topology: Literal["sno", "compact", "ha"] = "sno"
    storage: Literal["none", "lvms", "odf"] = "none"
    storage_disk_size: int = Field(100, ge=20, le=2048)  # GiB, extra disk per storage node (LVMS / ODF)
    operators: List[OperatorRequest] = []
    sriov: SriovOptions = SriovOptions()
    metallb: MetalLBOptions = MetalLBOptions()
    # Stop offering updates (clusterversion channel cleared)
    disable_updates: bool = True

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
    managed_by: Optional[str] = None  # "storage:lvms" | "storage:odf" | "sriov" | "metallb"
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
    kind: Literal["operator", "lvms", "odf", "sriov", "metallb", "metallb-demo"]
    operator: Optional[OperatorRequest] = None
    sriov: Optional[SriovOptions] = None
    metallb: Optional[MetalLBOptions] = None


class ScenarioCheck(BaseModel):
    name: str
    ok: Optional[bool] = None
    detail: str = ""


class MetalLBScenario(BaseModel):
    """State of the "MetalLB L2 lab" scenario for the diagram + checks"""
    enabled: bool
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
    assisted_status: Optional[str] = None
    assisted_info: Optional[str] = None
    progress: Optional[int] = None       # Assisted Service total %
    hosts: List[AssistedHost] = []
    version: Optional[str] = None
    cluster_operators: List[ClusterOperatorStatus] = []
    addons: List[Dict[str, Any]] = []    # [{kind, name, state: pending|installing|done|error, message}]
