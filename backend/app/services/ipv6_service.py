"""Dual stack lab groups (docs/ipv6.md): IPv6 /64 assignment, members' network-config, DHCPv6 leases

    group network   <prefix>/64 (a free /64 of IPV6_ULA_POOL, unique on the host)
    router          <prefix>::1, router advertisements (M flag, default route) + stateful DHCPv6 (dnsmasq)
    machines        <prefix>::<host number of their IPv4 written in decimal>: 10.42.7.21 -> <prefix>::21
                    (dhcp-host by MAC, AAAA record)
    WireGuard       tunnel subnet6 (/64), devices <subnet6>::<n> like their IPv4 tunnel address
    BGP             one IPv6 announce range (/64) when BGP and IPv6 are both on (MP-BGP, IPv6 sessions)

Why stateful DHCPv6 and not SLAAC: AAAA records and reservations need addresses the app knows before the
guest boots. SLAAC addresses depend on the guest OS (EUI-64 on networkd, stable-privacy on NetworkManager),
DHCPv6 reservations by MAC (dnsmasq reads the MAC of directly connected clients) give every OS the same
address, exactly like the IPv4 reservations. The router-side config is rendered by router_ipv6.
"""
import ipaddress
import re
from typing import Any, Dict, Iterable, List, Optional

import yaml
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Group
from app.schemas.group import BGPAnnounceRange, GroupSpec, nets_overlap

LEASE_PREFIX = 64


def _v6_nets_of(spec: Dict[str, Any]) -> List[ipaddress.IPv6Network]:
    """Every IPv6 network a stored group spec holds (network, WireGuard tunnel, announce ranges)"""
    spec = spec or {}
    router = spec.get("router") or {}
    nets = [((spec.get("network") or {}).get("ipv6") or {}).get("prefix"),
            (router.get("wireguard") or {}).get("subnet6")]
    nets += [r.get("prefix") for r in (router.get("bgp") or {}).get("announce_ranges") or []]
    return [ipaddress.IPv6Network(n) for n in nets if n and ":" in n]


def used_v6(db: Session, group_id: Optional[int], name: str) -> List[ipaddress.IPv6Network]:
    used: List[ipaddress.IPv6Network] = []
    for g in db.query(Group).all():
        if g.id == group_id or g.name == name or g.status == "missing":
            continue
        used += _v6_nets_of(g.spec)
    return used


def free_64(used: Iterable[ipaddress.IPv6Network]) -> str:
    pool = ipaddress.IPv6Network(settings.IPV6_ULA_POOL)
    used = list(used)
    if pool.prefixlen > LEASE_PREFIX:
        raise ValueError(f"IPV6_ULA_POOL {pool} must be /64 or larger")
    # the pool's first /64 is skipped: <pool>::/64 reads like the pool itself
    for i, net in enumerate(pool.subnets(new_prefix=LEASE_PREFIX)):
        if i == 0 and pool.prefixlen < LEASE_PREFIX:
            continue
        if not any(nets_overlap(net, u) for u in used):
            return str(net)
        if i > 65536:
            break
    raise ValueError(f"No free /64 left in IPV6_ULA_POOL {pool}")


def assign(db: Session, spec: GroupSpec, existing: Optional[GroupSpec], group_id: Optional[int]) -> None:
    """Fill in the IPv6 /64s the app assigns (group prefix, WireGuard subnet6, an IPv6 BGP announce range),
    kept from `existing`, unique on the host. Called by group_service.normalize after WireGuard / BGP."""
    v6 = spec.network.ipv6
    old_v6 = existing.network.ipv6 if existing is not None else None
    if v6 is None:
        return
    if v6.prefix is None and old_v6 is not None:
        v6.prefix = old_v6.prefix
    used = used_v6(db, group_id, spec.name)
    own: List[ipaddress.IPv6Network] = []

    def take(current: Optional[str], previous: Optional[str], label: str) -> str:
        if current is None:
            current = free_64(used + own)
        elif current != previous:
            clash = next((u for u in used if nets_overlap(ipaddress.IPv6Network(current), u)), None)
            if clash is not None:
                raise ValueError(f"{label} {current} overlaps {clash}, already used by another group on this host")
        own.append(ipaddress.IPv6Network(current))
        return current

    if not v6.enabled and v6.prefix is None:
        return  # disabled before ever being assigned: nothing to keep
    v6.prefix = take(v6.prefix, old_v6.prefix if old_v6 else None, "IPv6 prefix")
    if not v6.enabled:
        return

    wg = spec.router.wireguard
    if wg is not None:
        old_wg = existing.router.wireguard if existing is not None else None
        wg.subnet6 = wg.subnet6 or (old_wg.subnet6 if old_wg else None)
        wg.subnet6 = take(wg.subnet6, old_wg.subnet6 if old_wg else None, "IPv6 tunnel subnet")

    bgp = spec.router.bgp
    if bgp is not None:
        old_bgp = existing.router.bgp if existing is not None else None
        old_prefixes = {r.prefix for r in (old_bgp.announce_ranges if old_bgp else [])}
        for r in bgp.announce_ranges:
            if ":" in r.prefix:
                take(r.prefix, r.prefix if r.prefix in old_prefixes else None, "Announce range")
        had_v6 = existing is not None and existing.ipv6_prefix() is not None
        bgp_was_on = old_bgp is not None and old_bgp.enabled
        if bgp.enabled and not any(":" in r.prefix for r in bgp.announce_ranges) and not (had_v6 and bgp_was_on):
            # BGP and IPv6 both on for the first time: something to announce over IPv6 right away
            bgp.announce_ranges.append(BGPAnnounceRange(prefix=take(None, None, "Announce range"), name="lab6"))


# Members (cloud-init network-config v2)

def member_network_config(mac: str, distribution: str) -> str:
    """DHCPv4 + router advertisements + DHCPv6 on the member's NIC. Debian / Ubuntu (netplan, networkd):
    also on extra NICs like cloud_image_service.dhcp_all_network_config. EL (NetworkManager): only the
    primary NIC by MAC (NM's ipv6.method auto = RA + DHCPv6 when the RA says "managed")."""
    v6 = {"dhcp6": True, "accept-ra": True}
    primary = {"match": {"macaddress": mac.lower()}, "dhcp4": True, **v6}
    if distribution in ("debian", "ubuntu"):
        def others(pattern: str) -> Dict[str, Any]:  # separate dicts: no YAML anchors in the output
            return {"match": {"name": pattern}, "dhcp4": True, "optional": True,
                    "dhcp4-overrides": {"route-metric": 200}, **v6, "dhcp6-overrides": {"route-metric": 200}}
        ethernets = {"nic0": primary, "nicx": others("en*"), "nicy": others("eth*")}
    else:
        primary.pop("accept-ra")  # NM: method auto already accepts RAs
        ethernets = {"nic0": primary}
    return yaml.safe_dump({"version": 2, "ethernets": ethernets}, sort_keys=False)


# DHCPv6 leases (dnsmasq lease file: after a "duid <server duid>" line, "<expiry> <iaid> <address> <name|*> <duid>")

def _duid_mac(duid: str) -> Optional[str]:
    """MAC of a DUID-LLT (00:01, hw 00:01) or DUID-LL (00:03, hw 00:01) of an Ethernet client"""
    parts = duid.lower().split(":")
    if len(parts) == 14 and parts[:4] == ["00", "01", "00", "01"]:
        return ":".join(parts[8:])
    if len(parts) == 10 and parts[:4] == ["00", "03", "00", "01"]:
        return ":".join(parts[4:])
    return None


def parse_v6_leases(text: str, spec: GroupSpec) -> List[Dict[str, Any]]:
    """IPv6 leases of the dnsmasq lease file; the MAC comes from the reservation holding that address,
    else from the DUID when it embeds one ("" when it doesn't, e.g. networkd's DUID-EN)"""
    by_ip6: Dict[str, str] = {}
    for ip, mac in ([(m.ip, m.mac) for m in spec.members] + [(r.ip, r.mac) for r in spec.reservations]
                    + [(h.ip, h.mac) for h in spec.dhcp_hosts]):
        ip6 = spec.ip6_of(ip)
        if ip6 and mac:
            by_ip6[ip6] = mac
    leases = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] == "duid" or not re.match(r"^\d+$", parts[1]) or ":" not in parts[2]:
            continue
        try:
            ip = str(ipaddress.IPv6Address(parts[2]))
        except ValueError:
            continue
        duid = None if parts[4] == "*" else parts[4]
        leases.append({"expiry": int(parts[0]), "iaid": int(parts[1]), "ip": ip, "family": "ipv6",
                       "hostname": None if parts[3] == "*" else parts[3], "duid": duid,
                       "mac": by_ip6.get(ip) or (_duid_mac(duid) if duid else None) or ""})
    return leases


# Dual stack clusters (kubeadm, OpenShift): pods / services of both families, nodes on the group's /64

def group_ip6(gspec: Optional[Dict[str, Any]]) -> Optional[tuple]:
    """(group cidr, IPv6 prefix) of a stored group spec with IPv6 on, for ip6_for(); None otherwise"""
    v6 = ((gspec or {}).get("network") or {}).get("ipv6") or {}
    if not v6.get("enabled") or not v6.get("prefix"):
        return None
    return gspec.get("cidr"), v6["prefix"]


def is_dual_stack(cluster: Any) -> bool:
    spec = cluster.spec or {}
    return bool(spec.get("dual_stack") or (spec.get("openshift") or {}).get("dual_stack"))


def is_ipv6_only(cluster: Any) -> bool:
    """IPv6 single stack OpenShift (openshift.ip_family ipv6): nodes, pods and services IPv6 only"""
    return ((cluster.spec or {}).get("openshift") or {}).get("ip_family") == "ipv6"


def ip_family(cluster: Any) -> str:
    """ipv4 | dual_stack | ipv6 of any cluster"""
    return "ipv6" if is_ipv6_only(cluster) else "dual_stack" if is_dual_stack(cluster) else "ipv4"


def check_dual_stack(spec: GroupSpec, wanted: Optional[bool], cidrs: Dict[str, str],
                     pod_prefix: tuple = (48, 64), service_prefix: tuple = (108, 120)) -> bool:
    """Whether a new cluster in the group `spec` is dual stack: `wanted` (None = when the group has IPv6),
    and its IPv6 in-cluster networks `cidrs` ({label: cidr}, pods first then services) are valid: inside
    their prefix length limits, not overlapping each other, the group's IPv6 networks or IPV6_ULA_POOL
    (where the app takes every group /64)."""
    has_v6 = spec.network.ipv6 is not None and spec.network.ipv6.enabled
    if wanted and not has_v6:
        raise ValueError(f"Lab group {spec.name} has no IPv6: switch on IPv6 (dual stack) on the group first "
                         "(Network & DNS tab) or create the cluster single stack")
    if not (has_v6 if wanted is None else wanted):
        return False
    taken = [(f"the group's IPv6 network {n}", n) for n in _v6_nets_of(spec.model_dump())]
    taken.append((f"IPV6_ULA_POOL {settings.IPV6_ULA_POOL} (the lab groups' IPv6 networks)",
                  ipaddress.IPv6Network(settings.IPV6_ULA_POOL)))
    for (label, cidr), (lo, hi) in zip(cidrs.items(), (pod_prefix, service_prefix)):
        try:
            net = ipaddress.IPv6Network(cidr)
        except ValueError as e:
            raise ValueError(f"Invalid IPv6 {label} {cidr}: {e}")
        if not lo <= net.prefixlen <= hi:
            raise ValueError(f"{label} {cidr}: the prefix length must be between /{lo} and /{hi}")
        clash = next((t for t in taken if nets_overlap(net, t[1])), None)
        if clash is not None:
            raise ValueError(f"{label} {cidr} overlaps {clash[0]}")
        taken.append((f"{label} {cidr}", net))
    return True
