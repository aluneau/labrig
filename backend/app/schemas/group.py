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
same router config. vlans are part of the schema so specs can already carry
them, but routers reject them until they are implemented. wireguard (remote access)
and bgp (FRR) are implemented by the "el" router.
"""
import base64
import binascii
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


def _ipv6(value: Optional[str], label: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(ipaddress.IPv6Address(value.strip()))
    except ValueError:
        raise ValueError(f"{label} '{value}' is not an IPv6 address")


def _ip_any(value: Optional[str], label: str) -> Optional[str]:
    """An IPv4 or IPv6 address (normalized)"""
    if value is None:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise ValueError(f"{label} '{value}' is not an IP address")


def _net_any(value: str, label: str) -> str:
    """An IPv4 or IPv6 network (host bits cleared)"""
    try:
        return str(ipaddress.ip_network(value.strip(), strict=False))
    except ValueError:
        raise ValueError(f"{label} '{value}' is not an IP network")


def nets_overlap(a: Any, b: Any) -> bool:
    """overlaps() across families is meaningless (ipaddress compares the integers): False"""
    return a.version == b.version and a.overlaps(b)


def ip6_for(ipv4: Optional[str], net4: Optional[str], net6: Optional[str]) -> Optional[str]:
    """The IPv6 address paired with a group IPv4 address (dual stack): same host number, written in
    decimal digits so it reads alike: 10.42.7.21 in 10.42.7.0/24 -> <prefix>::21, .1 -> ::1"""
    if not ipv4 or not net4 or not net6:
        return None
    n4 = ipaddress.IPv4Network(net4)
    offset = int(ipaddress.IPv4Address(ipv4)) - int(n4.network_address)
    if offset <= 0:
        return None
    return str(ipaddress.IPv6Network(net6).network_address + int(str(offset), 16))


class DNSRecord(BaseModel):
    """A record served by the router. name is relative to the group domain
    ("api.ocp", "*.apps.ocp") unless it ends with a dot (absolute)."""
    name: str = Field(..., min_length=1, max_length=253)
    a: Optional[str] = None
    aaaa: Optional[str] = None  # IPv6 (with or without `a`); served even when the group has no IPv6
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

    @field_validator("aaaa")
    @classmethod
    def _aaaa(cls, v: Optional[str]) -> Optional[str]:
        return _ipv6(v, "AAAA record")

    @model_validator(mode="after")
    def _one_value(self):
        if (self.a is None and self.aaaa is None) == (self.cname is None):
            raise ValueError(f"DNS record '{self.name}': set 'a' and / or 'aaaa', or 'cname'")
        if self.cname and self.name.startswith("*."):
            raise ValueError(f"DNS record '{self.name}': wildcards can only be A / AAAA records")
        return self


ZONE_SERVER = re.compile(r"^(?P<ip>\d{1,3}(\.\d{1,3}){3})(#(?P<port>\d{1,5}))?$")


class DNSZone(BaseModel):
    """Conditional forwarding (split DNS): names in `domain` and below are resolved by `servers`
    (dnsmasq server=/domain/ip), everything else by the forwarders. A server is an IPv4 address,
    "ip#port", or the name of a group member / reserved host (e.g. a member running the customer's
    internal DNS): resolved to its address when the router config is rendered."""
    domain: str = Field(..., min_length=1, max_length=253)
    servers: List[str] = Field(..., min_length=1)
    # With router.dns.stop_rebind: private addresses answered for this zone are still accepted
    allow_private: bool = True

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        v = v.strip().rstrip(".").lower()
        if not DNS_NAME.match(v) or v.startswith("*"):
            raise ValueError(f"'{v}' is not a valid DNS zone")
        return v

    @field_validator("servers")
    @classmethod
    def _servers(cls, v: List[str]) -> List[str]:
        out = []
        for s in v:
            s = s.strip().lower()
            m = ZONE_SERVER.match(s)
            if m:
                _ipv4(m.group("ip"), "Zone server")
                if m.group("port") and not 1 <= int(m.group("port")) <= 65535:
                    raise ValueError(f"Zone server '{s}': bad port")
            elif not re.match(HOST_LABEL, s):
                raise ValueError(f"Zone server '{s}' must be an IPv4 address, ip#port or a member name")
            out.append(s)
        return out


class DNSSpec(BaseModel):
    # Upstream resolvers; empty = whatever the uplink's DHCP gives the router
    forwarders: List[str] = []
    records: List[DNSRecord] = []
    zones: List[DNSZone] = []  # split DNS (conditional forwarding), docs/router-cases.md
    # Knobs to reproduce resolver behaviour (all off = dnsmasq defaults)
    stop_rebind: bool = False   # drop private addresses in upstream answers (DNS rebinding protection)
    no_negcache: bool = False   # don't cache NXDOMAIN / NODATA answers
    cache_size: Optional[int] = Field(None, ge=0, le=100000)  # 0 = no cache (dnsmasq default 150)

    @field_validator("forwarders")
    @classmethod
    def _forwarders(cls, v: List[str]) -> List[str]:
        return [_ipv4(f, "Forwarder") for f in v]


# BGP on the router (FRR). vlans: accepted in the schema, rejected by the router backends for now

BGP_ROUTER_ASN = 64512   # the router
BGP_PEER_ASN = 64513     # what lab machines (cluster nodes, MetalLB speakers) use by default


class BGPNeighbor(BaseModel):
    """An explicit BGP peer of the router (machines of the group network also peer without one,
    through the listen range)"""
    ip: str
    asn: int = Field(..., ge=1, le=4294967295)
    name: Optional[str] = Field(None, pattern=HOST_LABEL)  # description only
    owner: Optional[str] = None

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        return _ip_any(v, "BGP neighbor")


class BGPAnnounceRange(BaseModel):
    """Prefixes the router accepts from its BGP peers (that prefix and anything more specific, e.g.
    the /32 of a MetalLB service). Routes for anything else are refused."""
    prefix: str
    name: Optional[str] = Field(None, pattern=HOST_LABEL)
    owner: Optional[str] = None  # "cluster:<name>" (MetalLB BGP pool): managed by the app

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        return _net_any(v, "Announce range")


class BGPSpec(BaseModel):
    """The router runs FRR (bgpd): every machine of the group network may open a BGP session to the
    router's LAN address (dynamic neighbors, `listen` + `peer_asn`), plus explicit `neighbors`.
    Accepted routes (inside `announce_ranges`) go into the router's routing table, several next hops
    for one prefix are all used (ECMP). The router announces nothing."""
    enabled: bool = True
    asn: int = Field(BGP_ROUTER_ASN, ge=1, le=4294967295)
    listen: bool = True  # accept sessions from any address of the group network
    peer_asn: Optional[int] = Field(BGP_PEER_ASN, ge=1, le=4294967295)  # None = any other ASN (eBGP)
    neighbors: List[BGPNeighbor] = []
    announce_ranges: List[BGPAnnounceRange] = []
    maximum_paths: int = Field(8, ge=1, le=64)

    def check(self, cidr: str, wg_subnet: Optional[str], net6: Optional[str] = None,
              wg_subnet6: Optional[str] = None) -> None:
        nets = [ipaddress.ip_network(n) for n in (cidr, net6) if n]
        ips = [n.ip for n in self.neighbors]
        dupes = {ip for ip in ips if ips.count(ip) > 1}
        if dupes:
            raise ValueError(f"Duplicate BGP neighbors: {', '.join(sorted(dupes))}")
        for n in self.neighbors:
            addr = ipaddress.ip_address(n.ip)
            if addr.version == 6 and net6 is None:
                continue  # kept (not rendered) while the group's IPv6 is off
            if not any(addr.version == net.version and addr in net for net in nets):
                raise ValueError(f"BGP neighbor {n.ip} is not on the group network "
                                 f"{' / '.join(str(net) for net in nets)}")
        prefixes = [ipaddress.ip_network(r.prefix) for r in self.announce_ranges]
        tunnels = [ipaddress.ip_network(n) for n in (wg_subnet, wg_subnet6) if n]
        for i, p in enumerate(prefixes):
            lan = next((net for net in nets if nets_overlap(p, net)), None)
            if lan is not None:
                raise ValueError(f"Announce range {p} overlaps the group network {lan}: "
                                 "BGP routes must be for addresses outside it")
            tunnel = next((t for t in tunnels if nets_overlap(p, t)), None)
            if tunnel is not None:
                raise ValueError(f"Announce range {p} overlaps the WireGuard tunnel subnet {tunnel}")
            clash = next((q for q in prefixes[:i] if nets_overlap(q, p)), None)
            if clash is not None:
                raise ValueError(f"Announce ranges {clash} and {p} overlap")


def _wg_key(value: str, label: str) -> str:
    """A WireGuard key: base64 of 32 bytes"""
    value = value.strip()
    try:
        ok = len(value) == 44 and len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        ok = False
    if not ok:
        raise ValueError(f"{label} '{value}' is not a WireGuard key (44 base64 characters)")
    return value


def _ipv4_net(value: str, label: str) -> str:
    try:
        return str(ipaddress.IPv4Network(value.strip(), strict=False))
    except ValueError:
        raise ValueError(f"{label} '{value}' is not an IPv4 network")


def _ipv6_64(value: str, label: str) -> str:
    try:
        net = ipaddress.IPv6Network(value.strip(), strict=False)
    except ValueError:
        raise ValueError(f"{label} '{value}' is not an IPv6 network")
    if net.prefixlen != 64:
        raise ValueError(f"{label} {net} must be a /64 (DHCPv6 + router advertisements)")
    return str(net)


class WireGuardPeer(BaseModel):
    """A device allowed to connect to the router's WireGuard (e.g. a laptop). Only its public key is
    stored: a key pair generated by the app is shown once, in the client config."""
    name: Optional[str] = Field(None, pattern=HOST_LABEL)  # assigned (peer<N>) when unset
    public_key: str
    ip: Optional[str] = None  # assigned by the app: tunnel address of the device
    endpoint: Optional[str] = None  # host:port, only for peers the router dials (site to site)
    allowed_ips: List[str] = []     # networks behind the peer, routed to it by the router

    @field_validator("public_key")
    @classmethod
    def _key(cls, v: str) -> str:
        return _wg_key(v, "Public key")

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: Optional[str]) -> Optional[str]:
        return _ipv4(v, "Peer IP")

    @field_validator("allowed_ips")
    @classmethod
    def _allowed(cls, v: List[str]) -> List[str]:
        return [_ipv4_net(n, "Allowed IPs") for n in v]


WG_RESERVED_PORTS = {53, 67, 68}


class WireGuardSpec(BaseModel):
    """Remote access to the group ("road warrior"): the router runs WireGuard (wg0) on `listen_port`,
    the host relays UDP `host_port` to it (the router's uplink is NATed by libvirt, unreachable from
    the LAN). Devices get an address of `subnet` and reach the group network, the router's DNS zone
    and its load balancers (uplink address)."""
    enabled: bool = True  # False keeps keys and peers, stops wg0
    listen_port: int = Field(51820, ge=1, le=65535)  # on the router
    # Assigned by the app
    host_port: Optional[int] = Field(None, ge=1024, le=65535)  # UDP port on the host (relay)
    subnet: Optional[str] = None      # tunnel subnet, the router takes its first address
    subnet6: Optional[str] = None     # IPv6 tunnel /64 (groups with IPv6), same host numbers as `subnet`
    public_key: Optional[str] = None  # the router's (its private key never leaves the router)
    peers: List[WireGuardPeer] = []

    @field_validator("subnet")
    @classmethod
    def _subnet(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        net = ipaddress.IPv4Network(_ipv4_net(v, "Tunnel subnet"))
        if not 16 <= net.prefixlen <= 29:
            raise ValueError("The tunnel subnet prefix must be between /16 and /29")
        return str(net)

    @field_validator("subnet6")
    @classmethod
    def _subnet6(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _ipv6_64(v, "IPv6 tunnel subnet")

    @field_validator("public_key")
    @classmethod
    def _key(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _wg_key(v, "Router public key")

    @field_validator("listen_port")
    @classmethod
    def _port(cls, v: int) -> int:
        if v in WG_RESERVED_PORTS:
            raise ValueError(f"WireGuard can't listen on UDP {v} (used by the router's DNS / DHCP)")
        return v

    def router_ip(self) -> Optional[str]:
        """The router's tunnel address (first address of the subnet)"""
        return str(next(ipaddress.IPv4Network(self.subnet).hosts())) if self.subnet else None

    def ip6_of(self, ipv4: Optional[str]) -> Optional[str]:
        """IPv6 tunnel address paired with a tunnel IPv4 address (None without subnet6)"""
        return ip6_for(ipv4, self.subnet, self.subnet6)

    def router_ip6(self) -> Optional[str]:
        return self.ip6_of(self.router_ip())

    def check(self, cidr: str) -> None:
        names = [p.name for p in self.peers if p.name]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"Duplicate WireGuard peer names: {', '.join(sorted(dupes))}")
        keys = [p.public_key for p in self.peers]
        if len(set(keys)) != len(keys):
            raise ValueError("Two WireGuard peers have the same public key")
        if self.public_key and self.public_key in keys:
            raise ValueError("A WireGuard peer can't use the router's public key")
        if self.subnet is None:
            return
        net = ipaddress.IPv4Network(self.subnet)
        if net.overlaps(ipaddress.IPv4Network(cidr)):
            raise ValueError(f"The tunnel subnet {net} overlaps the group network {cidr}")
        ips = [p.ip for p in self.peers if p.ip]
        dupes = {ip for ip in ips if ips.count(ip) > 1}
        if dupes:
            raise ValueError(f"Duplicate WireGuard peer IPs: {', '.join(sorted(dupes))}")
        for p in self.peers:
            if p.ip is None:
                continue
            addr = ipaddress.IPv4Address(p.ip)
            if addr not in net or addr in (net.network_address, net.broadcast_address) or p.ip == self.router_ip():
                raise ValueError(f"WireGuard peer {p.name or p.public_key}: {p.ip} is not a free address of {net}")


class IPv6Spec(BaseModel):
    """Dual stack (docs/ipv6.md): the group network also gets an IPv6 /64 (a ULA of IPV6_ULA_POOL unless set).
    The router takes <prefix>::1 and sends router advertisements (default route, M flag) + stateful DHCPv6:
    every machine with an IPv4 reservation gets the IPv6 address with the same host number
    (10.42.7.21 -> <prefix>::21) and an AAAA record. The uplink stays IPv4 only: no IPv6 internet.
    egress: what happens to IPv6 packets from the lab towards anything outside the lab's IPv6 networks:
    reject (fails at once: no route / prohibited) or drop (silently dropped: connections hang until they
    time out, the classic "AAAA preferred, IPv6 broken" symptom)."""
    enabled: bool = True
    prefix: Optional[str] = None  # assigned by the app when unset
    egress: Literal["reject", "drop"] = "reject"

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not str(v).strip():
            return None
        return _ipv6_64(v, "IPv6 prefix")


class VLANSpec(BaseModel):
    id: int = Field(..., ge=1, le=4094)
    cidr: str


PROXY_USER = r"^[A-Za-z0-9._-]{1,64}$"
PROXY_PASSWORD = r"^[A-Za-z0-9._~!*+=,;-]{1,128}$"  # no ':' '@' '/' (proxy URLs) nor shell / squid specials


class ProxySpec(BaseModel):
    """Egress mode "proxy": squid on the router, the only way out for the lab (docs/router-cases.md).
    Plain forward proxy, CONNECT for HTTPS (no TLS interception)."""
    port: int = Field(3128, ge=1, le=65535)
    # Destinations allowed through the proxy (squid dstdomain: ".example.com" = the domain and below);
    # empty = any
    allow_domains: List[str] = []
    connect_ports: List[int] = [443]  # CONNECT (HTTPS tunnels) only to these ports
    username: Optional[str] = Field(None, pattern=PROXY_USER)  # basic auth when set (with password)
    password: Optional[str] = Field(None, pattern=PROXY_PASSWORD)
    # Members created from a cloud image get the proxy environment (http(s)_proxy / no_proxy in
    # /etc/environment + profile.d, apt / dnf proxy). Off = reproduce an unconfigured host
    member_env: bool = True

    @field_validator("allow_domains")
    @classmethod
    def _domains(cls, v: List[str]) -> List[str]:
        out: List[str] = []
        for d in v:
            d = d.strip().lower().rstrip(".")
            bare = d.lstrip(".")
            if not bare or not DNS_NAME.match(bare) or bare.startswith("*"):
                raise ValueError(f"Proxy allowed domain '{d}' is not a domain ('.example.com' = it and below)")
            if d not in out:
                out.append(d)
        return out

    @field_validator("connect_ports")
    @classmethod
    def _ports(cls, v: List[int]) -> List[int]:
        if not v or any(not 1 <= p <= 65535 for p in v):
            raise ValueError("connect_ports: at least one port, 1-65535")
        return sorted(set(v))

    @model_validator(mode="after")
    def _auth(self):
        if bool(self.username) != bool(self.password):
            raise ValueError("Proxy authentication needs both a username and a password")
        if self.port in RESERVED_ROUTER_PORTS:
            raise ValueError(f"The proxy can't listen on port {self.port} (used by the router itself)")
        return self


class EgressSpec(BaseModel):
    """What the group's machines may reach outside the lab (forwarded traffic through the router).
    blocked: everything from the group network to the uplink side is refused except `allow` (e.g. a
    customer proxy); the router itself (DNS forwarding, NTP, registry mirroring), WireGuard devices,
    load balancers and BGP-announced addresses keep working. proxy: the same, plus squid on the router
    (`proxy`) as the only way out. Applied live."""
    mode: Literal["open", "blocked", "proxy"] = "open"
    allow: List[str] = []  # CIDRs / addresses still reachable when blocked / proxy
    proxy: ProxySpec = ProxySpec()

    @field_validator("allow")
    @classmethod
    def _allow(cls, v: List[str]) -> List[str]:
        out: List[str] = []
        for n in v:
            n = _net_any(n, "Egress allow")
            if n not in out:
                out.append(n)
        return out


REGISTRY_DISK_SERIAL = "vmm-registry"  # /dev/disk/by-id/virtio-vmm-registry on the router


class RegistrySpec(BaseModel):
    """Mirror registry on the router (Red Hat mirror-registry = Quay + podman, filled by oc-mirror v2).
    Enabled, the router gets memory_mb / vcpus (instead of router.memory / vcpu) and an extra thin disk
    of disk_gb for the registry storage and oc-mirror's cache (docs/disconnected.md)."""
    enabled: bool = False
    port: int = Field(8443, ge=1, le=65535)
    disk_gb: int = Field(250, ge=20, le=16384)
    memory_mb: int = Field(8192, ge=2048)
    vcpus: int = Field(4, ge=1, le=64)
    # Assigned / read back by the app
    hostname: Optional[str] = None  # registry.<domain> (DNS record served by the router)
    ca_pem: Optional[str] = None    # CA of the registry's TLS certificate (made on the router)

    @field_validator("port")
    @classmethod
    def _port(cls, v: int) -> int:
        if v in RESERVED_ROUTER_PORTS:
            raise ValueError(f"The registry can't listen on port {v} (used by the router itself)")
        return v


class PathSpec(BaseModel):
    """The router as a narrow hop between the lab and the outside (docs/router-cases.md): `mtu` on its
    uplink and LAN interfaces (members keep the group network's MTU), the ICMP "fragmentation needed"
    messages it must send can be dropped (PMTUD black hole), and TCP MSS clamping works around it. Live."""
    mtu: Optional[int] = Field(None, ge=576, le=1500)
    drop_frag_needed: bool = False  # PMTUD black hole: the router never tells senders the path is smaller
    clamp_mss: bool = False         # rewrite the MSS of forwarded SYNs to the route MTU (the usual fix)


class NetworkSpec(BaseModel):
    """The group network itself. mtu: libvirt <mtu> of the bridge, the router's LAN, and DHCP option 26
    for the members (default 1500). Changes reach the members at their next DHCP renewal / boot; the
    bridge's own MTU (libvirt) follows at the next group start."""
    mtu: Optional[int] = Field(None, ge=576, le=9000)
    ipv6: Optional[IPv6Spec] = None  # dual stack (docs/ipv6.md)


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
    egress: EgressSpec = EgressSpec()
    registry: RegistrySpec = RegistrySpec()
    path: PathSpec = PathSpec()
    # Assigned by the app
    ip: Optional[str] = None
    lan_mac: Optional[str] = Field(None, pattern=MAC)
    uplink_mac: Optional[str] = Field(None, pattern=MAC)
    # Fixed address of the uplink NIC (DHCP reservation on the uplink network): how the host
    # reaches the router's load balancers
    uplink_ip: Optional[str] = None

    def effective_memory(self) -> int:
        """RAM the router VM gets (MiB): more when it runs the mirror registry"""
        return max(self.memory, self.registry.memory_mb) if self.registry.enabled else self.memory

    def effective_vcpu(self) -> int:
        return max(self.vcpu, self.registry.vcpus) if self.registry.enabled else self.vcpu


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
    """A member VM. Quick members are a cloud image + the group's cloud-init; "custom" ones
    (the full Create VM form) can also boot an ISO or an empty disk: those get no cloud-init,
    their OS gets the reserved IP and name by DHCP from the router (dhcp-host on the MAC)."""
    name: str = Field(..., pattern=LABEL, description="Member name, also its hostname (<name>.<domain>)")
    # Boot source: cloud_image (image, configured by cloud-init) | iso (install from `iso`) | empty disk
    source: Literal["cloud_image", "iso", "empty"] = "cloud_image"
    image: Optional[str] = "debian-13"  # cloud_image only (cleared for the other sources)
    iso: Optional[str] = None           # iso only: ISO volume path or name (see GET /storage/isos)
    memory: int = Field(1024, ge=128)  # MiB
    vcpu: int = Field(1, ge=1, le=512)
    disk_size: int = Field(10, ge=0)   # GiB (0 = no disk, iso only)
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

    @model_validator(mode="after")
    def _source(self):
        if self.source == "iso":
            if not self.iso:
                raise ValueError(f"Member {self.name}: choose the ISO to boot ('iso')")
        elif self.iso:
            raise ValueError(f"Member {self.name}: 'iso' is only used with source 'iso'")
        if self.source == "cloud_image" and not self.image:
            raise ValueError(f"Member {self.name}: choose a cloud image ('image')")
        if self.source != "iso" and self.disk_size < 1:
            raise ValueError(f"Member {self.name}: the disk size must be at least 1 GiB")
        if self.source != "cloud_image":
            self.image = None
        return self


class DHCPRange(BaseModel):
    start: str
    end: str


HOSTNAME = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"


class DHCPHostSpec(BaseModel):
    """Static DHCP reservation for a non-member machine on the group network (e.g. a VM attached
    from the VMs page). With a hostname, the router also serves <hostname>.<domain>."""
    mac: str = Field(..., pattern=MAC)
    ip: str
    hostname: Optional[str] = Field(None, pattern=HOSTNAME)

    @field_validator("mac", mode="before")
    @classmethod
    def _mac(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        return _ipv4(v, "Reservation IP")

    @field_validator("hostname", mode="before")
    @classmethod
    def _hostname(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip().lower()
            return v or None
        return v


class AddressPoolSpec(BaseModel):
    """A range of the group network kept free for something inside the guests (e.g. a cluster's
    MetalLB pool): never handed out by DHCP nor assigned to members / reservations."""
    name: str = Field(..., pattern=HOST_LABEL)
    start: str
    end: str
    owner: Optional[str] = None  # "cluster:<name>"

    @field_validator("start", "end")
    @classmethod
    def _ip(cls, v: str) -> str:
        return _ipv4(v, "Address pool bound")

    def contains(self, ip: str) -> bool:
        return ipaddress.IPv4Address(self.start) <= ipaddress.IPv4Address(ip) <= ipaddress.IPv4Address(self.end)


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
    dhcp_hosts: List[DHCPHostSpec] = []  # static reservations for non-member machines
    address_pools: List[AddressPoolSpec] = []  # ranges kept free (MetalLB pools...)
    network: NetworkSpec = NetworkSpec()  # MTU, IPv6 (dual stack)

    def ipv6_prefix(self) -> Optional[str]:
        """The group's IPv6 /64 when dual stack is on (enabled and assigned)"""
        v6 = self.network.ipv6
        return v6.prefix if v6 is not None and v6.enabled and v6.prefix else None

    def ip6_of(self, ipv4: Optional[str]) -> Optional[str]:
        """IPv6 address paired with a group IPv4 address (None when the group has no IPv6)"""
        return ip6_for(ipv4, self.cidr, self.ipv6_prefix())

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
        for reserved in ("router", "rtr"):
            if reserved in names:
                raise ValueError(f"'{reserved}' is reserved for the group's router")
        for label, ip in ([("Router IP", self.router.ip)] + [(f"Member {m.name} IP", m.ip) for m in self.members]
                          + [(f"Reserved host {r.name} IP", r.ip) for r in self.reservations]):
            if ip is None:
                continue
            addr = ipaddress.IPv4Address(ip)
            if addr not in net or addr in (net.network_address, net.broadcast_address):
                raise ValueError(f"{label} {ip} is not a usable address of {net}")
        owners: Dict[str, List[str]] = {}
        if self.router.ip:
            owners[self.router.ip] = ["router"]
        for m in self.members:
            if m.ip:
                owners.setdefault(m.ip, []).append(m.name)
        for r in self.reservations:
            owners.setdefault(r.ip, []).append(r.name)
        dupes = [f"{ip} ({', '.join(names)})" for ip, names in sorted(owners.items()) if len(names) > 1]
        if dupes:
            raise ValueError(f"Duplicate IPs: {'; '.join(dupes)}")
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
        self._check_dhcp_hosts(net)
        reg = self.router.registry
        if reg.enabled:
            if reg.port in lb_ports:
                raise ValueError(f"The registry port {reg.port} is already used by a load balancer")
            if "registry" in names + [h.hostname for h in self.dhcp_hosts] + records:
                raise ValueError("The name 'registry' is used by the group's mirror registry")
        self._check_router_cases(names, lb_ports)
        if self.router.wireguard is not None:
            self.router.wireguard.check(self.cidr)
        if self.router.bgp is not None:
            wg = self.router.wireguard
            self.router.bgp.check(self.cidr, wg.subnet if wg else None, self.ipv6_prefix(),
                                  wg.subnet6 if wg else None)
        return self

    def _check_router_cases(self, names: List[str], lb_ports: List[int]) -> None:
        """Split DNS zones, proxy port, MTUs (docs/router-cases.md)"""
        zones = [z.domain for z in self.router.dns.zones]
        dupes = {z for z in zones if zones.count(z) > 1}
        if dupes:
            raise ValueError(f"Duplicate DNS zones: {', '.join(sorted(dupes))}")
        hosts = set(names) | {h.hostname for h in self.dhcp_hosts if h.hostname}
        for z in self.router.dns.zones:
            if z.domain == self.domain:
                raise ValueError(f"DNS zone {z.domain} is the group's own domain (served by the router)")
            for server in z.servers:
                if not ZONE_SERVER.match(server) and server not in hosts:
                    raise ValueError(f"DNS zone {z.domain}: server '{server}' is neither an address nor a member / host name")
        egress = self.router.egress
        if egress.mode == "proxy":
            port = egress.proxy.port
            if port in lb_ports:
                raise ValueError(f"The proxy port {port} is already used by a load balancer")
            if self.router.registry.enabled and port == self.router.registry.port:
                raise ValueError(f"The proxy port {port} is the registry's")
        path_mtu, lan_mtu = self.router.path.mtu, self.network.mtu
        if path_mtu and lan_mtu and path_mtu > lan_mtu:
            raise ValueError(f"router.path.mtu ({path_mtu}) can't be larger than the network MTU ({lan_mtu})")

    def _check_dhcp_hosts(self, net: ipaddress.IPv4Network) -> None:
        """Reservations: usable address of the subnet (inside the dynamic range is fine: dnsmasq never
        hands a reserved address to another client), no clash with the router, members or each other"""
        owners: Dict[str, str] = {}  # ip / mac / hostname -> who uses it
        if self.router.ip:
            owners[self.router.ip] = "the router"
        for mac in (self.router.lan_mac, self.router.uplink_mac):
            if mac:
                owners[mac] = "the router"
        owners["router"] = "the router"
        for m in self.members:
            for key in (m.ip, m.mac, m.name):
                if key:
                    owners[key] = f"member {m.name}"
        for r in self.reservations:  # cluster nodes
            for key in (r.ip, r.mac, r.name):
                owners[key] = f"{r.owner or 'reserved host'} ({r.name})"
        for h in self.dhcp_hosts:
            addr = ipaddress.IPv4Address(h.ip)
            if addr not in net or addr in (net.network_address, net.broadcast_address):
                raise ValueError(f"Reservation {h.mac}: {h.ip} is not a usable address of {net}")
            if owners.get(h.mac) == f"the reservation for {h.mac}":
                raise ValueError(f"Duplicate reservation for {h.mac}")
            for label, key in (("MAC", h.mac), ("IP", h.ip), ("hostname", h.hostname)):
                if key and key in owners:
                    raise ValueError(f"Reservation {h.mac}: {label} {key} is already used by {owners[key]}")
            if h.hostname and any(r.name == h.hostname for r in self.router.dns.records):
                raise ValueError(f"Reservation {h.mac}: hostname {h.hostname} is already a DNS record")
            owners[h.mac] = owners[h.ip] = f"the reservation for {h.mac}"
            if h.hostname:
                owners[h.hostname] = f"the reservation for {h.mac}"


# API responses

class GroupMemberInfo(BaseModel):
    name: str
    role: str
    hostname: Optional[str] = None
    fqdn: Optional[str] = None
    ip: Optional[str] = None
    ip6: Optional[str] = None  # dual stack groups
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
    mac: str                        # "" for a DHCPv6 lease whose MAC is unknown (see duid)
    family: str = "ipv4"            # ipv4 | ipv6 (DHCPv6)
    duid: Optional[str] = None      # DHCPv6 client DUID
    iaid: Optional[int] = None      # DHCPv6 IA id
    hostname: Optional[str] = None
    expiry: Optional[int] = None  # epoch seconds, 0 = infinite
    kind: str = "dynamic"           # member | reservation | dynamic
    member: Optional[str] = None    # member name (kind == member)
    vm_name: Optional[str] = None   # VM with this MAC on the group network, if any
    vm_running: bool = False


class LeaseRelease(BaseModel):
    mac: str
    ip: str
    released: bool


class GroupHostInfo(BaseModel):
    """A reserved host (not a member), e.g. a cluster node"""
    name: str
    ip: str
    ip6: Optional[str] = None
    mac: str
    owner: Optional[str] = None
    fqdn: Optional[str] = None
    vm_id: Optional[int] = None
    state: str = "missing"


class GroupClusterRef(BaseModel):
    id: int
    name: str
    type: str


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


class ProxyInfo(BaseModel):
    url: str            # http://<router ip>:<port>
    url_with_auth: str  # with user:password when the proxy needs it
    fqdn_url: str       # http://router.<domain>:<port>
    no_proxy: str
    env: Dict[str, str]  # http_proxy, https_proxy, no_proxy (+ upper case)


class GroupDetail(Group):
    router_uplink_ips: List[str] = []
    leases: List[GroupLease] = []  # read from the router's dnsmasq through the guest agent
    proxy: Optional[ProxyInfo] = None  # egress mode proxy: URL + client environment


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


# WireGuard remote access

class WireGuardSettings(BaseModel):
    """Enable / disable remote access; ports default to the current (or assigned) ones"""
    enabled: bool = True
    listen_port: Optional[int] = Field(None, ge=1, le=65535)
    host_port: Optional[int] = Field(None, ge=1024, le=65535)


class WireGuardPeerCreate(BaseModel):
    name: str = Field(..., pattern=HOST_LABEL, description="Device name, e.g. laptop")
    # Omitted: the app generates the key pair and returns the private key once, in the client config
    public_key: Optional[str] = None
    endpoint_host: Optional[str] = Field(None, max_length=253, description="Host name / address in the client config")
    allowed_ips: List[str] = []  # networks behind the peer (site to site); empty for a laptop

    @field_validator("public_key")
    @classmethod
    def _key(cls, v: Optional[str]) -> Optional[str]:
        v = v.strip() if v else None
        return _wg_key(v, "Public key") if v else None

    @field_validator("allowed_ips")
    @classmethod
    def _allowed(cls, v: List[str]) -> List[str]:
        return [_ipv4_net(n, "Allowed IPs") for n in v]


class WireGuardPeerInfo(BaseModel):
    name: str
    public_key: str
    ip: Optional[str] = None
    ip6: Optional[str] = None  # groups with IPv6
    allowed_ips: List[str] = []
    # Live, from `wg show wg0 dump` on the router
    endpoint: Optional[str] = None       # where the router last saw it (the host's relay)
    latest_handshake: Optional[int] = None  # epoch seconds, 0 = never
    rx_bytes: Optional[int] = None
    tx_bytes: Optional[int] = None


class WireGuardStatus(BaseModel):
    configured: bool = False  # spec has a wireguard block
    enabled: bool = False
    listen_port: Optional[int] = None
    host_port: Optional[int] = None
    subnet: Optional[str] = None
    subnet6: Optional[str] = None
    router_tunnel_ip: Optional[str] = None
    router_tunnel_ip6: Optional[str] = None
    public_key: Optional[str] = None
    endpoint_host: str = ""     # default host in client configs (WG_ENDPOINT_HOST or the host's LAN address)
    endpoint: Optional[str] = None
    client_allowed_ips: List[str] = []  # what a client routes through the tunnel
    relay_listening: bool = False
    relay_error: Optional[str] = None
    host_port_range: str = ""  # WG_HOST_PORTS (what setup.sh opens in the host firewall)
    firewall: Optional[str] = None  # active host firewall (ufw / firewalld) that must allow host_port/udp
    router_running: bool = False
    router_error: Optional[str] = None  # wg0 not up on the router, or peers not readable
    peers: List[WireGuardPeerInfo] = []


class WireGuardPeerCreated(BaseModel):
    peer: WireGuardPeerInfo
    config: str               # wg-quick client config (with the private key only when generated)
    filename: str
    has_private_key: bool
    warning: Optional[str] = None  # saved, but not applied on the router yet (e.g. it is stopped)


# BGP (FRR on the router)

class BGPSettings(BaseModel):
    """PUT /groups/{id}/bgp: unset fields keep their value. announce_ranges / neighbors replace the
    user's own entries (entries owned by a cluster always stay)."""
    enabled: bool = True
    asn: Optional[int] = Field(None, ge=1, le=4294967295)
    listen: Optional[bool] = None
    peer_asn: Optional[int] = Field(None, ge=1, le=4294967295)
    any_peer_asn: bool = False  # accept any other ASN from the group network (remote-as external)
    maximum_paths: Optional[int] = Field(None, ge=1, le=64)
    announce_ranges: Optional[List[BGPAnnounceRange]] = None
    neighbors: Optional[List[BGPNeighbor]] = None


class BGPSession(BaseModel):
    peer: str
    afi: str = "ipv4"                   # ipv4 | ipv6 (address family of the session)
    name: Optional[str] = None          # member / node owning that address
    remote_as: Optional[int] = None
    state: str                          # Established, Active, Connect, Idle...
    established: bool = False
    uptime: Optional[str] = None
    uptime_seconds: Optional[int] = None
    prefixes_received: Optional[int] = None
    dynamic: bool = False               # accepted through the listen range
    description: Optional[str] = None


class BGPNextHop(BaseModel):
    ip: Optional[str] = None
    name: Optional[str] = None
    interface: Optional[str] = None
    active: bool = False


class BGPRoute(BaseModel):
    prefix: str
    installed: bool = False   # in the router's kernel routing table
    selected: bool = False
    nexthops: List[BGPNextHop] = []


class BGPStatus(BaseModel):
    configured: bool = False
    enabled: bool = False
    asn: Optional[int] = None
    router_ip: Optional[str] = None
    router_ip6: Optional[str] = None
    listen: bool = False
    listen_range: Optional[str] = None
    listen_range6: Optional[str] = None
    peer_asn: Optional[int] = None
    maximum_paths: Optional[int] = None
    neighbors: List[BGPNeighbor] = []
    announce_ranges: List[BGPAnnounceRange] = []
    router_running: bool = False
    router_error: Optional[str] = None
    frr_version: Optional[str] = None
    sessions: List[BGPSession] = []
    routes: List[BGPRoute] = []


# Topology view (GET /groups/{id}/topology): everything the diagram needs, live

class TopologyMachine(BaseModel):
    """A machine on the group network: member, cluster node or reserved host"""
    kind: str                       # member | node | reservation
    name: str
    vm_name: Optional[str] = None
    vm_id: Optional[int] = None
    role: Optional[str] = None      # member role, or ctlplane / worker
    cluster: Optional[str] = None
    ip: Optional[str] = None
    ip6: Optional[str] = None
    mac: Optional[str] = None
    fqdn: Optional[str] = None
    state: str = "missing"
    bgp_state: Optional[str] = None  # its BGP session with the router, if any
    bgp_prefixes: List[str] = []      # prefixes the router routes to it (learned by BGP)
    l2_announces: List[str] = []      # service IPs it answers ARP for (MetalLB L2)


class TopologyVip(BaseModel):
    """A floating address: a service IP (MetalLB) or a BGP-learned prefix"""
    address: str                    # 10.45.0.1 or 10.45.0.0/27
    kind: str                       # metallb-l2 | metallb-bgp | bgp-route
    name: Optional[str] = None      # e.g. hello
    hostname: Optional[str] = None  # hello.<domain>
    cluster: Optional[str] = None
    via: List[str] = []             # machine names that carry it (ARP owner or BGP next hops)


class TopologyCluster(BaseModel):
    id: int
    name: str
    type: str
    status: str
    nodes: List[str] = []
    api_url: Optional[str] = None
    console_url: Optional[str] = None
    load_balancer_ports: List[int] = []
    metallb_enabled: bool = False
    metallb_mode: Optional[str] = None   # l2 | bgp
    metallb_pool: Optional[str] = None
    service_ip: Optional[str] = None
    service_hostname: Optional[str] = None
    apps_domain: Optional[str] = None


class TopologyLoadBalancer(BaseModel):
    name: str
    port: int
    backends: List[str] = []
    owner: Optional[str] = None


class TopologyRouter(BaseModel):
    name: str
    vm_id: Optional[int] = None
    state: str = "missing"
    lan_ip: Optional[str] = None
    lan_ip6: Optional[str] = None
    uplink_ip: Optional[str] = None
    uplink_network: Optional[str] = None
    tunnel_ip: Optional[str] = None
    roles: List[str] = []           # dhcp, dns, nat, ntp, lb, wireguard, bgp
    dhcp_range: Optional[str] = None
    dns_records: int = 0
    dns_forwarders: List[str] = []
    load_balancers: List[TopologyLoadBalancer] = []
    config_applied: bool = False
    # router cases (docs/router-cases.md)
    proxy_port: Optional[int] = None  # egress mode proxy
    dns_zones: List[str] = []         # split DNS
    network_mtu: int = 1500
    lan_mtu: int = 1500
    path_mtu: Optional[int] = None
    drop_frag_needed: bool = False
    clamp_mss: bool = False


class TopologyWireGuardPeer(BaseModel):
    name: str
    ip: Optional[str] = None
    ip6: Optional[str] = None
    latest_handshake: Optional[int] = None
    endpoint: Optional[str] = None


class TopologyWireGuard(BaseModel):
    enabled: bool = False
    subnet: Optional[str] = None
    subnet6: Optional[str] = None
    router_ip: Optional[str] = None
    router_ip6: Optional[str] = None
    host_port: Optional[int] = None
    listen_port: Optional[int] = None
    endpoint: Optional[str] = None
    relay_listening: bool = False
    client_allowed_ips: List[str] = []
    peers: List[TopologyWireGuardPeer] = []


class GroupTopology(BaseModel):
    id: int
    name: str
    cidr: str
    ipv6_prefix: Optional[str] = None
    domain: str
    network_name: str
    state: str
    status: str
    router: TopologyRouter
    wireguard: TopologyWireGuard
    bgp: BGPStatus
    machines: List[TopologyMachine] = []
    clusters: List[TopologyCluster] = []
    vips: List[TopologyVip] = []
    address_pools: List[AddressPoolSpec] = []
    errors: List[str] = []
