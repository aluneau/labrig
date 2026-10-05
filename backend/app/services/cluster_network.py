"""Where cluster nodes live: addressing (fixed MAC -> IP) and name publishing (DNS)

`ClusterNetwork` is the seam between clusters and the network layer. v1 is
`LibvirtClusterNetwork`: a libvirt network (one created per cluster, NAT, or an
existing one chosen by the user) whose dnsmasq hands out the reservations and
serves the records. `GroupClusterNetwork` implements the same interface on a lab
group: reservations / DNS records / an API load balancer owned by the cluster are
added to the group spec and served by the group's router VM (future-features §3.2).
"""
import ipaddress
import logging
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from typing import Dict, Iterable, List, Optional, Set
from xml.sax.saxutils import escape, quoteattr

import libvirt

from app.config import settings
from app.libvirt_client import libvirt_client

logger = logging.getLogger(__name__)

OWNED_PREFIX = "vmm-k-"


class ClusterNetwork(ABC):
    """Network the nodes of one cluster are attached to"""

    #: libvirt network name node NICs attach to
    name: str
    #: created for (and deleted with) the cluster
    owned: bool

    @abstractmethod
    def ensure(self) -> None:
        """Create the network if it is ours, and make sure it is running"""

    @abstractmethod
    def allocate_ip(self, taken: Iterable[str] = ()) -> str:
        """A free address for a new node (not reserved, not leased, not in `taken`)"""

    @abstractmethod
    def reserve(self, hostname: str, mac: str, ip: str) -> None:
        """Pin mac -> ip (DHCP reservation)"""

    @abstractmethod
    def release(self, mac: str) -> None:
        """Drop the reservation of mac (no error if there is none)"""

    @abstractmethod
    def publish(self, ip: str, hostnames: List[str]) -> None:
        """Make hostnames resolve to ip for the nodes (replaces the names of ip)"""

    @abstractmethod
    def unpublish(self, ip: str) -> None:
        """Remove the records of ip (no error if there are none)"""

    @abstractmethod
    def gateway(self) -> Optional[str]:
        """Address of the network's router / DNS server"""

    @abstractmethod
    def subnet(self) -> ipaddress.IPv4Network:
        ...

    @abstractmethod
    def destroy(self) -> None:
        """Delete the network if it is ours (otherwise only our records are removed by the caller)"""

    # Optional parts (defaults fit a plain libvirt network)

    #: the network has a router that can front the API with a load balancer (lab groups)
    fronts_api: bool = False

    def commit(self) -> None:
        """Apply the reservations / records changed since the last commit (no-op when they apply
        immediately, as on a libvirt network)"""

    def publish_api(self, hostnames: List[str], ctlplane_ips: List[str], port: int) -> str:
        """Make hostnames resolve to a load balancer listening on `port` in front of the control
        planes (:6443); returns the address the host uses to reach it"""
        raise NotImplementedError

    def free_lb_port(self, start: int = 6443) -> int:
        return start

    def stop(self) -> None:
        """Called after the cluster's nodes are shut down"""


class LibvirtClusterNetwork(ClusterNetwork):
    """v1: libvirt network + its dnsmasq (DHCP reservations through net.update, <dns><host> records)"""

    def __init__(self, name: str, owned: bool, cidr: Optional[str] = None, zone: Optional[str] = None):
        self.name = name
        self.owned = owned
        self.cidr = cidr  # only used to create an owned network
        self.zone = zone  # DNS domain of an owned network (<cluster>.<domain>)

    # creation

    @staticmethod
    def owned_name(cluster_name: str) -> str:
        return OWNED_PREFIX + cluster_name

    @classmethod
    def for_new_cluster(cls, cluster_name: str, zone: str, cidr: Optional[str],
                        avoid: Iterable[str] = ()) -> "LibvirtClusterNetwork":
        """Owned NAT network vmm-k-<cluster>; picks a free /24 when cidr is None"""
        if cidr:
            try:
                net = ipaddress.IPv4Network(cidr, strict=False)
            except ValueError as e:
                raise ValueError(f"Invalid cidr: {e}")
            if net.prefixlen > 26:
                raise ValueError("The cluster network must be a /26 or larger")
            clash = next((n for n in used_subnets() if n.overlaps(net)), None)
            if clash:
                raise ValueError(f"{net} overlaps the existing network {clash}")
        else:
            net = free_subnet(avoid)
        return cls(OWNED_PREFIX + cluster_name, owned=True, cidr=str(net), zone=zone)

    @classmethod
    def existing(cls, name: str) -> "LibvirtClusterNetwork":
        network = cls(name, owned=False)
        root = network._xml()
        if network._ip_element(root) is None:
            raise ValueError(f"Network '{name}' has no IPv4 subnet")
        if root.find("./ip/dhcp") is None:
            raise ValueError(f"Network '{name}' has no DHCP server: nodes need reservations")
        dns = root.find("dns")
        if dns is not None and dns.get("enable") == "no":
            raise ValueError(f"Network '{name}' has its DNS server disabled")
        return network

    def ensure(self) -> None:
        conn = libvirt_client.connect()
        try:
            net = conn.networkLookupByName(self.name)
        except libvirt.libvirtError:
            if not self.owned:
                raise ValueError(f"Network '{self.name}' does not exist")
            libvirt_client.create_network(self.name, self._owned_xml(), autostart=True)
            logger.info(f"Created cluster network {self.name} ({self.cidr})")
            return
        if not net.isActive():
            net.create()

    def _owned_xml(self) -> str:
        net = ipaddress.IPv4Network(self.cidr)
        hosts = list(net.hosts())
        # Reservations get the low addresses (.10+), the dynamic range is the upper half
        dyn_start, dyn_end = hosts[len(hosts) // 2], hosts[-1]
        return f"""<network>
  <name>{escape(self.name)}</name>
  <forward mode='nat'/>
  <domain name={quoteattr(self.zone)} localOnly='yes'/>
  <ip address='{hosts[0]}' prefix='{net.prefixlen}'>
    <dhcp><range start='{dyn_start}' end='{dyn_end}'/></dhcp>
  </ip>
</network>"""

    # addressing

    def _xml(self) -> ET.Element:
        return ET.fromstring(libvirt_client.get_network_xml(self.name))

    @staticmethod
    def _ip_element(root: ET.Element) -> Optional[ET.Element]:
        return next((el for el in root.findall("ip") if el.get("family", "ipv4") == "ipv4"), None)

    def subnet(self) -> ipaddress.IPv4Network:
        ip = self._ip_element(self._xml())
        prefix = ip.get("prefix") or ipaddress.IPv4Network(f"0.0.0.0/{ip.get('netmask')}").prefixlen
        return ipaddress.IPv4Network(f"{ip.get('address')}/{prefix}", strict=False)

    def gateway(self) -> Optional[str]:
        ip = self._ip_element(self._xml())
        return ip.get("address") if ip is not None else None

    def allocate_ip(self, taken: Iterable[str] = ()) -> str:
        root = self._xml()
        ip_el = self._ip_element(root)
        subnet = self.subnet()
        used: Set[str] = set(taken) | {ip_el.get("address")}
        used |= {h.get("ip") for h in ip_el.findall("./dhcp/host") if h.get("ip")}
        used |= {h.get("ip") for h in root.findall("./dns/host") if h.get("ip")}
        try:
            used |= {lease["ipaddr"] for lease in libvirt_client.get_network_leases(self.name)}
        except libvirt.libvirtError:
            pass
        ranges = [(ipaddress.IPv4Address(r.get("start")), ipaddress.IPv4Address(r.get("end")))
                  for r in ip_el.findall("./dhcp/range")]

        def in_range(addr) -> bool:
            return any(start <= addr <= end for start, end in ranges)

        # Skip the first 9 addresses (gateway, appliances); prefer addresses outside the dynamic range
        candidates = [a for a in list(subnet.hosts())[9:] if str(a) not in used]
        candidates.sort(key=lambda a: (in_range(a), a))
        if not candidates:
            raise ValueError(f"No free address left in {subnet} on network '{self.name}'")
        return str(candidates[0])

    def reserve(self, hostname: str, mac: str, ip: str) -> None:
        self.release(mac)
        libvirt_client.update_dhcp_host(self.name, "add", mac, ip, hostname)

    def release(self, mac: str) -> None:
        try:
            root = self._xml()
        except libvirt.libvirtError:
            return  # network gone
        for host in root.findall("./ip/dhcp/host"):
            if (host.get("mac") or "").lower() == mac.lower():
                libvirt_client.update_dhcp_host(self.name, "delete", host.get("mac"), host.get("ip"),
                                                host.get("name"))

    # names

    def publish(self, ip: str, hostnames: List[str]) -> None:
        self.unpublish(ip)
        libvirt_client.update_dns_host(self.name, "add", ip, hostnames)

    def unpublish(self, ip: str) -> None:
        try:
            root = self._xml()
        except libvirt.libvirtError:
            return
        for host in root.findall("./dns/host"):
            if host.get("ip") == ip:
                libvirt_client.update_dns_host(self.name, "delete", ip,
                                               [h.text for h in host.findall("hostname") if h.text])

    def destroy(self) -> None:
        if self.owned:
            libvirt_client.delete_network(self.name)


def used_subnets() -> List[ipaddress.IPv4Network]:
    """IPv4 subnets of every libvirt network (active or not)"""
    result = []
    for net in libvirt_client.list_networks():
        root = ET.fromstring(net["xml"])
        for ip in root.findall("ip"):
            if ip.get("family", "ipv4") != "ipv4" or not ip.get("address"):
                continue
            prefix = ip.get("prefix") or ipaddress.IPv4Network(f"0.0.0.0/{ip.get('netmask', '255.255.255.0')}").prefixlen
            result.append(ipaddress.IPv4Network(f"{ip.get('address')}/{prefix}", strict=False))
    return result


def free_subnet(avoid: Iterable[str] = ()) -> ipaddress.IPv4Network:
    """First /24 of CLUSTER_SUBNET_POOL overlapping no libvirt network nor `avoid` (pod/service CIDRs)"""
    taken = used_subnets() + [ipaddress.IPv4Network(c, strict=False) for c in avoid]
    pool = ipaddress.IPv4Network(settings.CLUSTER_SUBNET_POOL, strict=False)
    for candidate in pool.subnets(new_prefix=24) if pool.prefixlen <= 24 else [pool]:
        if not any(candidate.overlaps(t) for t in taken):
            return candidate
    raise ValueError(f"No free /24 left in CLUSTER_SUBNET_POOL ({pool}): give a cidr")


class GroupClusterNetwork(ClusterNetwork):
    """Nodes inside a lab group. Reservations, DNS records and the API load balancer go into the
    group spec as entries owned by "cluster:<name>"; commit() saves them and pushes the router
    config live (dnsmasq + haproxy). The group (network + router) belongs to the group, so
    destroy() only removes the cluster's entries; an auto-created group is deleted by the cluster."""

    fronts_api = True

    def __init__(self, group_id: int, cluster_name: str, owned: bool):
        self.group_id = group_id
        self.cluster_name = cluster_name
        self.owner = f"cluster:{cluster_name}"
        self.owned = owned  # auto-created for the cluster: deleted with it
        self._ops: List = []
        group = self._group()
        self.group_name = group.name if group else None
        self.name = f"vmm-g-{self.group_name}" if group else ""

    # helpers

    @staticmethod
    def _db():
        from app.database import SessionLocal
        return SessionLocal()

    def _group(self):
        from app.models import Group
        db = self._db()
        try:
            group = db.query(Group).filter(Group.id == self.group_id).first()
            if group is not None:
                db.expunge(group)
            return group
        finally:
            db.close()

    def spec(self):
        from app.schemas.group import GroupSpec
        group = self._group()
        if group is None:
            raise ValueError("The cluster's lab group no longer exists")
        return GroupSpec.model_validate(group.spec)

    def _relative(self, hostname: str, domain: str) -> Optional[str]:
        """DNS record name for the group's zone: relative to its domain, absolute otherwise"""
        if "." not in hostname:
            return None  # short names: the reservation already gives <name>.<domain>
        if hostname.endswith("." + domain):
            return hostname[: -len(domain) - 1]
        return hostname + "."

    # ClusterNetwork

    def ensure(self) -> None:
        from app.services.group_service import group_service
        db = self._db()
        try:
            group_service.ensure_running(db, self.group_id)
        finally:
            db.close()

    def allocate_ip(self, taken: Iterable[str] = ()) -> str:
        from app.services.group_service import group_service
        return group_service.free_ip(self.spec(), taken)

    def reserve(self, hostname: str, mac: str, ip: str) -> None:
        from app.schemas.group import ReservationSpec

        def op(spec):
            spec.reservations = [r for r in spec.reservations
                                 if not (r.owner == self.owner and (r.mac == mac.lower() or r.name == hostname))]
            spec.reservations.append(ReservationSpec(name=hostname, mac=mac, ip=ip, owner=self.owner))
        self._ops.append(op)

    def release(self, mac: str) -> None:
        def op(spec):
            spec.reservations = [r for r in spec.reservations if not (r.owner == self.owner and r.mac == mac.lower())]
        self._ops.append(op)

    def publish(self, ip: str, hostnames: List[str]) -> None:
        from app.schemas.group import DNSRecord

        def op(spec):
            names = [n for n in (self._relative(h, spec.domain) for h in hostnames) if n]
            records = [r for r in spec.router.dns.records
                       if not (r.owner == self.owner and (r.a == ip or r.name in names))]
            if any(r.name in names for r in records):
                clash = next(r.name for r in records if r.name in names)
                raise ValueError(f"DNS record '{clash}' already exists in group {spec.name}")
            spec.router.dns.records = records + [DNSRecord(name=n, a=ip, owner=self.owner) for n in names]
        self._ops.append(op)

    def unpublish(self, ip: str) -> None:
        def op(spec):
            spec.router.dns.records = [r for r in spec.router.dns.records
                                       if not (r.owner == self.owner and r.a == ip)]
        self._ops.append(op)

    def publish_api(self, hostnames: List[str], ctlplane_ips: List[str], port: int) -> str:
        from app.schemas.group import LoadBalancerSpec
        spec = self.spec()
        self.publish(spec.router.ip, hostnames)
        lb_name = f"{self.cluster_name}-api"

        def op(spec):
            spec.load_balancers = [lb for lb in spec.load_balancers
                                   if not (lb.owner == self.owner and lb.name == lb_name)]
            spec.load_balancers.append(LoadBalancerSpec(
                name=lb_name, port=port, backends=[f"{ip}:6443" for ip in ctlplane_ips], owner=self.owner))
        self._ops.append(op)
        if not spec.router.uplink_ip:
            raise ValueError(f"The router of group {spec.name} has no uplink address: the host can't reach the API")
        return spec.router.uplink_ip

    def free_lb_port(self, start: int = 6443) -> int:
        from app.schemas.group import RESERVED_ROUTER_PORTS
        used = {lb.port for lb in self.spec().load_balancers} | RESERVED_ROUTER_PORTS
        port = start
        while port in used:
            port += 1
        return port

    def commit(self) -> None:
        if not self._ops:
            return
        from app.services.group_service import group_service
        ops, self._ops = self._ops, []

        def mutate(spec):
            for op in ops:
                op(spec)
        db = self._db()
        try:
            group_service.update_owned(db, self.group_id, mutate)
        finally:
            db.close()

    def remove_all(self) -> None:
        """Drop every entry the cluster owns in the group"""
        def op(spec):
            spec.reservations = [r for r in spec.reservations if r.owner != self.owner]
            spec.router.dns.records = [r for r in spec.router.dns.records if r.owner != self.owner]
            spec.load_balancers = [lb for lb in spec.load_balancers if lb.owner != self.owner]
        self._ops.append(op)
        self.commit()

    def gateway(self) -> Optional[str]:
        return self.spec().router.ip

    def subnet(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.spec().cidr)

    def uplink_ip(self) -> Optional[str]:
        return self.spec().router.uplink_ip

    def stop(self) -> None:
        """An auto-created group only serves its cluster: shut its router down with it"""
        if not self.owned or self._group() is None:
            return
        from app.services.group_service import group_service, router_vm_name
        group_service._shutdown(router_vm_name(self.group_name), False)

    def destroy(self) -> None:
        if self._group() is None:
            return
        if not self.owned:
            self.remove_all()
