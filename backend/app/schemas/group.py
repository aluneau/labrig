"""Lab group schemas

GroupSpec is the declarative description of a lab (also what OpenTofu sends):

    name: case-12345
    cidr: 10.42.7.0/24
    domain: case-12345.lab
    uplink: default
    router: {flavour: el, dns: {forwarders: [1.1.1.1], records: [{name: api.ocp, a: 10.42.7.10}]}}
    members: [{name: web1, image: debian-13, memory: 1024, ip: 10.42.7.21}]

Fields marked "assigned by the app" are filled in when the group is created
(fixed MACs, member IPs) and kept in the stored spec, so a re-render gives the
same router config. bgp / wireguard / vlans are part of the schema so specs can
already carry them, but routers reject them until they are implemented.
"""
import ipaddress
import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LABEL = r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"
HOST_LABEL = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
# Router RAM (MiB). 512 is enough with the 1 GiB swap file cloud-init creates before installing
# packages (dnf at first boot is the peak); see router_service.
ROUTER_MEMORY_DEFAULT = 512
# Ports the router itself uses: load balancers can't take them
RESERVED_ROUTER_PORTS = {22, 53, 67, 68}
MAC = r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$"
DNS_NAME = re.compile(r"^(\*\.)?([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)*"
                      r"[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.?$")


def _ipv4(value: Optional[str], label: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except ValueError:
        raise ValueError(f"{label} '{value}' is not an IPv4 address")


class DNSRecord(BaseModel):
    """A record served by the router. name is relative to the group domain
    ("api.ocp", "*.apps.ocp") unless it ends with a dot (absolute)."""
    name: str = Field(..., min_length=1, max_length=253)
    a: Optional[str] = None
    cname: Optional[str] = None
    # Set by the app for records it manages (e.g. "cluster:k1"); users can't change those
    owner: Optional[str] = None

    @field_validator("name", "cname")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not DNS_NAME.match(v):
            raise ValueError(f"'{v}' is not a valid DNS name")
        return v

    @field_validator("a")
    @classmethod
    def _a(cls, v: Optional[str]) -> Optional[str]:
        return _ipv4(v, "A record")

    @model_validator(mode="after")
    def _one_value(self):
        if (self.a is None) == (self.cname is None):
            raise ValueError(f"DNS record '{self.name}': set exactly one of 'a' or 'cname'")
        if self.cname and self.name.startswith("*."):
            raise ValueError(f"DNS record '{self.name}': wildcards can only be A records")
        return self


class DNSSpec(BaseModel):
    # Upstream resolvers; empty = whatever the uplink's DHCP gives the router
    forwarders: List[str] = []
    records: List[DNSRecord] = []

    @field_validator("forwarders")
    @classmethod
    def _forwarders(cls, v: List[str]) -> List[str]:
        return [_ipv4(f, "Forwarder") for f in v]


# v2 blocks: accepted in the schema, rejected by the router backends for now

class BGPNeighbor(BaseModel):
    ip: str
    asn: int = Field(..., ge=1, le=4294967295)


class BGPSpec(BaseModel):
    asn: int = Field(..., ge=1, le=4294967295)
    neighbors: List[BGPNeighbor] = []


class WireGuardPeer(BaseModel):
    public_key: str
    endpoint: Optional[str] = None
    allowed_ips: List[str] = []


class WireGuardSpec(BaseModel):
    listen_port: int = Field(51820, ge=1, le=65535)
    peers: List[WireGuardPeer] = []


class VLANSpec(BaseModel):
    id: int = Field(..., ge=1, le=4094)
    cidr: str


class RouterSpec(BaseModel):
    flavour: Literal["el", "vyos"] = "el"
    # Cloud image: "almalinux-9" (distribution-version) or a cloudimg-*.qcow2 volume name.
    # Default: the first ready EL image (AlmaLinux 9/10, Rocky 9, CentOS Stream).
    image: Optional[str] = None
    memory: int = Field(ROUTER_MEMORY_DEFAULT, ge=256)  # MiB
    vcpu: int = Field(1, ge=1, le=64)
    disk_size: int = Field(10, ge=5)   # GiB
    dns: DNSSpec = DNSSpec()
    bgp: Optional[BGPSpec] = None
    wireguard: Optional[WireGuardSpec] = None
    vlans: List[VLANSpec] = []
    # Assigned by the app
    ip: Optional[str] = None
    lan_mac: Optional[str] = Field(None, pattern=MAC)
    uplink_mac: Optional[str] = Field(None, pattern=MAC)
    # Fixed address of the uplink NIC (DHCP reservation on the uplink network): how the host
    # reaches the router's load balancers
    uplink_ip: Optional[str] = None


class ReservationSpec(BaseModel):
    """A host on the group network that is not a group member (e.g. a cluster node created by the
    cluster driver): the router gives it a static lease and the DNS name <name>.<domain>."""
    name: str = Field(..., pattern=HOST_LABEL)
    mac: str = Field(..., pattern=MAC)
    ip: str
    owner: Optional[str] = None  # "cluster:<name>": managed by the app

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        return _ipv4(v, "Reservation IP")

    @field_validator("mac", mode="before")
    @classmethod
    def _mac(cls, v: str) -> str:
        return v.lower() if isinstance(v, str) else v


class LoadBalancerSpec(BaseModel):
    """TCP load balancer on the router (haproxy): listens on every router address (LAN and uplink)
    on `port` and spreads connections over the healthy backends (round robin, TCP health checks)."""
    name: str = Field(..., pattern=HOST_LABEL)
    port: int = Field(..., ge=1, le=65535)
    backends: List[str] = Field(..., min_length=1)  # "ip:port"
    mode: Literal["tcp"] = "tcp"
    owner: Optional[str] = None

    @field_validator("backends")
    @classmethod
    def _backends(cls, v: List[str]) -> List[str]:
        result = []
        for backend in v:
            host, _, port = backend.strip().rpartition(":")
            if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
                raise ValueError(f"Backend '{backend}' must be ip:port")
            result.append(f"{_ipv4(host, 'Backend')}:{int(port)}")
        return result


class CloudInitSpec(BaseModel):
    """Login settings for the group's VMs (members may override them)"""
    username: Optional[str] = Field("admin", pattern=r"^[a-z_][a-z0-9_-]*$")
    password: Optional[str] = None
    ssh_keys: List[str] = []
    keyboard: Optional[str] = Field(None, pattern=r"^[a-z]{2,10}$")


class MemberSpec(BaseModel):
    name: str = Field(..., pattern=LABEL, description="Member name, also its hostname (<name>.<domain>)")
    image: str = "debian-13"
    memory: int = Field(1024, ge=128)  # MiB
    vcpu: int = Field(1, ge=1, le=512)
    disk_size: int = Field(10, ge=1)   # GiB
    role: str = Field("member", pattern=r"^[a-z0-9_-]{1,32}$")
    # Assigned by the app when unset
    ip: Optional[str] = None
    mac: Optional[str] = Field(None, pattern=MAC)
    cloud_init: Optional[CloudInitSpec] = None  # None = the group's cloud_init
    user_data: Optional[str] = None             # raw #cloud-config, replaces cloud_init

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: Optional[str]) -> Optional[str]:
        return _ipv4(v, "Member IP")

    @field_validator("mac", mode="before")
    @classmethod
    def _mac(cls, v: Optional[str]) -> Optional[str]:
        return v.lower() if isinstance(v, str) else v


class DHCPRange(BaseModel):
    start: str
    end: str


class GroupSpec(BaseModel):
    name: str = Field(..., pattern=LABEL, description="Lowercase letters, digits and '-', max 32 chars")
    cidr: str = Field(..., description="IPv4 subnet of the group network, e.g. 10.42.7.0/24")
    domain: Optional[str] = None       # default: <name>.lab
    uplink: Optional[str] = "default"  # libvirt network for the router's eth0; None = no uplink
    dhcp: Optional[DHCPRange] = None   # dynamic range for unknown clients; default: upper part of the subnet
    router: RouterSpec = RouterSpec()
    cloud_init: CloudInitSpec = CloudInitSpec()
    members: List[MemberSpec] = []
    # Hosts / load balancers / DNS records with an owner are managed by the app (cluster nodes,
    # API load balancer): kept as they are when a user replaces the spec
    reservations: List[ReservationSpec] = []
    load_balancers: List[LoadBalancerSpec] = []
    owner: Optional[str] = None  # "cluster:<name>" for a group created for (and deleted with) a cluster

    @field_validator("cidr")
    @classmethod
    def _cidr(cls, v: str) -> str:
        try:
            net = ipaddress.IPv4Network(v.strip(), strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid CIDR: {e}")
        if not 16 <= net.prefixlen <= 28:
            raise ValueError("The prefix must be between /16 and /28")
        return str(net)

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip().rstrip(".").lower()
        if not DNS_NAME.match(v) or v.startswith("*"):
            raise ValueError(f"'{v}' is not a valid domain")
        return v

    @model_validator(mode="after")
    def _consistency(self):
        if not self.domain:
            self.domain = f"{self.name}.lab"
        net = ipaddress.IPv4Network(self.cidr)
        names = [m.name for m in self.members] + [r.name for r in self.reservations]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"Duplicate member / reserved host names: {', '.join(sorted(dupes))}")
        if "router" in names:
            raise ValueError("'router' is reserved for the group's router")
        for label, ip in ([("Router IP", self.router.ip)] + [(f"Member {m.name} IP", m.ip) for m in self.members]
                          + [(f"Reserved host {r.name} IP", r.ip) for r in self.reservations]):
            if ip is None:
                continue
            addr = ipaddress.IPv4Address(ip)
            if addr not in net or addr in (net.network_address, net.broadcast_address):
                raise ValueError(f"{label} {ip} is not a usable address of {net}")
        ips = [m.ip for m in self.members if m.ip] + [r.ip for r in self.reservations]
        if self.router.ip:
            ips.append(self.router.ip)
        dupes = {ip for ip in ips if ips.count(ip) > 1}
        if dupes:
            raise ValueError(f"Duplicate IPs: {', '.join(sorted(dupes))}")
        if self.dhcp:
            for label, ip in (("DHCP start", self.dhcp.start), ("DHCP end", self.dhcp.end)):
                if ipaddress.IPv4Address(_ipv4(ip, label)) not in net:
                    raise ValueError(f"{label} {ip} is outside {net}")
            if ipaddress.IPv4Address(self.dhcp.start) > ipaddress.IPv4Address(self.dhcp.end):
                raise ValueError("DHCP start must be before DHCP end")
        records = [r.name for r in self.router.dns.records]
        dupes = {r for r in records if records.count(r) > 1}
        if dupes:
            raise ValueError(f"Duplicate DNS records: {', '.join(sorted(dupes))}")
        macs = [m.mac for m in self.members if m.mac] + [r.mac for r in self.reservations]
        dupes = {m for m in macs if macs.count(m) > 1}
        if dupes:
            raise ValueError(f"Duplicate MACs: {', '.join(sorted(dupes))}")
        lb_names = [lb.name for lb in self.load_balancers]
        lb_ports = [lb.port for lb in self.load_balancers]
        if len(set(lb_names)) != len(lb_names):
            raise ValueError("Duplicate load balancer names")
        dupes = {str(p) for p in lb_ports if lb_ports.count(p) > 1}
        if dupes:
            raise ValueError(f"Several load balancers on port {', '.join(sorted(dupes))}")
        reserved = sorted(str(p) for p in set(lb_ports) & RESERVED_ROUTER_PORTS)
        if reserved:
            raise ValueError(f"Port {', '.join(reserved)} is used by the router itself")
        return self


# API responses

class GroupMemberInfo(BaseModel):
    name: str
    role: str
    hostname: Optional[str] = None
    fqdn: Optional[str] = None
    ip: Optional[str] = None
    mac: Optional[str] = None
    vm_id: Optional[int] = None
    vm_name: Optional[str] = None
    vm_uuid: Optional[str] = None
    state: str = "missing"  # libvirt domain state, or "missing"
    image: Optional[str] = None
    memory: Optional[int] = None
    vcpu: Optional[int] = None


class GroupLease(BaseModel):
    ip: str
    mac: str
    hostname: Optional[str] = None
    expiry: Optional[int] = None  # epoch seconds, 0 = infinite


class Group(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    cidr: str
    domain: str
    uplink: Optional[str] = None
    status: str             # creating | ready | updating | error | deleting | missing
    state: str = "unknown"  # running | stopped | partial (live, from libvirt)
    error_message: Optional[str] = None
    network_name: str
    network_id: Optional[int] = None
    router: GroupMemberInfo
    members: List[GroupMemberInfo] = []
    member_count: int = 0
    hosts: List[GroupHostInfo] = []        # reserved hosts (cluster nodes)
    clusters: List[GroupClusterRef] = []   # clusters whose nodes live in this group
    config_applied: bool = False
    config_applied_at: Optional[datetime] = None
    config_error: Optional[str] = None
    spec: GroupSpec
    created_at: datetime
    updated_at: datetime


class GroupHostInfo(BaseModel):
    """A reserved host (not a member), e.g. a cluster node"""
    name: str
    ip: str
    mac: str
    owner: Optional[str] = None
    fqdn: Optional[str] = None
    vm_id: Optional[int] = None
    state: str = "missing"


class GroupClusterRef(BaseModel):
    id: int
    name: str
    type: str


class GroupDetail(Group):
    router_uplink_ips: List[str] = []
    leases: List[GroupLease] = []  # read from the router's dnsmasq through the guest agent


class GroupCreateResult(BaseModel):
    group: Group
    task_id: int


class GroupTaskResult(BaseModel):
    group: Group
    task_id: Optional[int] = None


class RouterConfig(BaseModel):
    flavour: str
    user_data: str
    network_config: str
    files: Dict[str, str]
    apply_command: str
    config_applied: bool = False
    config_applied_at: Optional[datetime] = None
    config_error: Optional[str] = None


class GroupExport(BaseModel):
    yaml: str
    spec: Dict[str, Any]
