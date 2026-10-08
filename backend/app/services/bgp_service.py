"""BGP on lab group routers (FRR bgpd), see docs/bgp.md

    node / member --eBGP (AS 64513)--> router LAN IP (AS 64512, dynamic neighbors on the group CIDR)
        "send traffic for 10.45.0.5/32 to me"  -> router kernel route 10.45.0.5 via node1, node2 (ECMP)

The spec's router.bgp block is the source of truth. Announce ranges (what the router accepts) are unique on
the host (BGP_ANNOUNCE_POOL), so a WireGuard client connected to several labs routes each to its own group.
Live state is read from the router with vtysh through the guest agent.
"""
import ipaddress
import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

import libvirt
from sqlalchemy.orm import Session

from app.config import settings
from app.libvirt_client import libvirt_client
from app.models import Group
from app.schemas.group import BGPAnnounceRange, BGPSpec, GroupSpec

logger = logging.getLogger(__name__)

RANGE_PREFIX = 27
SEPARATOR = "@@vmm@@"


def _bgp_of(spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return ((spec or {}).get("router") or {}).get("bgp")


def used_networks(db: Session, group_id: Optional[int], name: Optional[str],
                  taken_subnets: Iterable[str]) -> List[ipaddress.IPv4Network]:
    """Everything an announce range must not overlap on this host: other groups' CIDRs, tunnels and
    announce ranges, libvirt network subnets"""
    used = [ipaddress.IPv4Network(c) for c in taken_subnets]
    for g in db.query(Group).all():
        if g.id == group_id or g.name == name or g.status == "missing":
            continue
        used.append(ipaddress.IPv4Network(g.cidr))
        router = (g.spec or {}).get("router") or {}
        if (router.get("wireguard") or {}).get("subnet"):
            used.append(ipaddress.IPv4Network(router["wireguard"]["subnet"]))
        for r in (_bgp_of(g.spec) or {}).get("announce_ranges") or []:
            if ":" not in r["prefix"]:  # IPv6 ranges: ipv6_service
                used.append(ipaddress.IPv4Network(r["prefix"]))
    return used


def free_range(used: List[ipaddress.IPv4Network], prefix: int = RANGE_PREFIX) -> str:
    pool = ipaddress.IPv4Network(settings.BGP_ANNOUNCE_POOL)
    for net in pool.subnets(new_prefix=max(prefix, pool.prefixlen)):
        if not any(net.overlaps(u) for u in used):
            return str(net)
    raise ValueError(f"No free /{prefix} left in BGP_ANNOUNCE_POOL {settings.BGP_ANNOUNCE_POOL}")


def assign(db: Session, spec: GroupSpec, old: Optional[BGPSpec], group_id: Optional[int],
           taken_subnets: List[str]) -> None:
    """Check announce ranges against the host; a newly enabled block without ranges gets one /27 of
    BGP_ANNOUNCE_POOL (so lab machines have something to announce right away)"""
    bgp = spec.router.bgp
    if bgp is None:
        return
    used = used_networks(db, group_id, spec.name, taken_subnets)
    wg = spec.router.wireguard
    if wg is not None and wg.subnet:
        used.append(ipaddress.IPv4Network(wg.subnet))
    if not [r for r in bgp.announce_ranges if ":" not in r.prefix] and (old is None or not old.enabled) and bgp.enabled:
        bgp.announce_ranges.append(BGPAnnounceRange(prefix=free_range(used + [ipaddress.IPv4Network(spec.cidr)]),
                                                    name="lab"))
    old_prefixes = {r.prefix for r in (old.announce_ranges if old else [])}
    for r in bgp.announce_ranges:
        if r.prefix in old_prefixes or ":" in r.prefix:  # IPv6 ranges are checked by ipv6_service
            continue
        clash = next((u for u in used if ipaddress.IPv4Network(r.prefix).overlaps(u)), None)
        if clash is not None:
            raise ValueError(f"Announce range {r.prefix} overlaps {clash}, already used on this host")


# Router side (through the guest agent)

def _uptime(peer: Dict[str, Any]) -> Optional[int]:
    ms = peer.get("peerUptimeMsec")
    return int(ms) // 1000 if isinstance(ms, (int, float)) and ms else None


def _addr_key(value: str) -> Tuple[int, int]:
    try:
        addr = ipaddress.ip_address(value)
        return addr.version, int(addr)
    except ValueError:
        return 9, 0


def parse_summary(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`show bgp summary json` -> sessions (IPv4 unicast, then IPv6 unicast: MP-BGP, docs/ipv6.md)"""
    families = [("ipv4", data.get("ipv4Unicast") or data.get("ipv4") or {}),
                ("ipv6", data.get("ipv6Unicast") or data.get("ipv6") or {})]
    if not families[0][1] and not families[1][1] and "peers" in data:
        families = [("ipv4", data)]
    sessions = []
    for afi, af in families:
        sessions += _sessions(afi, af)
    return sessions


def _sessions(afi: str, af: Dict[str, Any]) -> List[Dict[str, Any]]:
    sessions = []
    for ip, p in sorted((af.get("peers") or {}).items(), key=lambda kv: _addr_key(kv[0])):
        state = p.get("state") or "unknown"
        sessions.append({
            "peer": ip, "afi": afi, "remote_as": p.get("remoteAs"), "state": state,
            "established": state == "Established",
            "uptime": p.get("peerUptime"), "uptime_seconds": _uptime(p),
            "prefixes_received": p.get("pfxRcd", p.get("acceptedPrefixCount")) if state == "Established" else 0,
            "dynamic": bool(p.get("dynamicPeer")),
            "description": p.get("desc") or p.get("hostname"),
        })
    return sessions


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def parse_routes(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`show ip route bgp json` -> [{prefix, installed, nexthops: [{ip, interface, active}]}]"""
    routes = []
    for prefix, entries in data.items():
        for e in entries if isinstance(entries, list) else []:
            if e.get("protocol") not in (None, "bgp"):
                continue
            hops = [{"ip": h.get("ip"), "interface": h.get("interfaceName"),
                     "active": bool(h.get("active")) and h.get("fib", True) is not False}
                    for h in e.get("nexthops") or [] if h.get("ip")]
            routes.append({"prefix": prefix, "installed": bool(e.get("installed")),
                           "selected": bool(e.get("selected")), "nexthops": hops})
    routes.sort(key=lambda r: (ipaddress.ip_network(r["prefix"]).version, ipaddress.ip_network(r["prefix"])))
    return routes


def read_status(vm_name: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """Sessions + BGP routes of the routing table, read with vtysh on the router"""
    script = ("if ! systemctl is-active -q frr; then echo 'FRR is not running on the router' >&2; exit 3; fi; "
              "vtysh -c 'show bgp summary json' && echo " + SEPARATOR + " && vtysh -c 'show ip route bgp json'"
              " && echo " + SEPARATOR + " && vtysh -c 'show version' | head -n 1"
              " && echo " + SEPARATOR + " && vtysh -c 'show ipv6 route bgp json'")
    try:
        result = libvirt_client.agent_exec(vm_name, "/bin/sh", ["-c", script], timeout=15)
    except (libvirt.libvirtError, TimeoutError, KeyError, ValueError) as e:
        return {}, f"Cannot read the BGP state of the router: {e}"
    if result["exitcode"] != 0:
        return {}, (result.get("stderr") or result.get("stdout") or "").strip()[-300:] or "vtysh failed"
    parts = result["stdout"].split(SEPARATOR)
    try:
        summary = json.loads(parts[0] or "{}")
        routes = json.loads(parts[1] or "{}") if len(parts) > 1 else {}
        routes.update(json.loads(parts[3] or "{}") if len(parts) > 3 else {})
    except ValueError as e:
        return {}, f"Unexpected vtysh output: {e}"
    version = parts[2].strip() if len(parts) > 2 else None
    return {"sessions": parse_summary(summary), "routes": parse_routes(routes), "frr_version": version}, None


def status(spec: GroupSpec, vm_name: str, running: bool) -> Dict[str, Any]:
    """GET /groups/{id}/bgp"""
    bgp = spec.router.bgp
    if bgp is None:
        return {"configured": False, "enabled": False, "router_running": running, "router_ip": spec.router.ip,
                "router_ip6": spec.ip6_of(spec.router.ip)}
    live: Dict[str, Any] = {}
    error = None
    if running and bgp.enabled:
        live, error = read_status(vm_name)
    names = _peer_names(spec)
    sessions = live.get("sessions") or []
    for s in sessions:
        s["name"] = names.get(s["peer"])
    routes = live.get("routes") or []
    for r in routes:
        for h in r["nexthops"]:
            h["name"] = names.get(h["ip"])
    return {
        "configured": True, "enabled": bgp.enabled, "asn": bgp.asn, "router_ip": spec.router.ip,
        "router_ip6": spec.ip6_of(spec.router.ip),
        "listen": bgp.listen, "listen_range": spec.cidr if bgp.listen else None, "peer_asn": bgp.peer_asn,
        "listen_range6": spec.ipv6_prefix() if bgp.listen else None,
        "maximum_paths": bgp.maximum_paths,
        "neighbors": [n.model_dump() for n in bgp.neighbors],
        "announce_ranges": [r.model_dump() for r in bgp.announce_ranges],
        "router_running": running, "router_error": error, "frr_version": live.get("frr_version"),
        "sessions": sessions, "routes": routes,
    }


def _peer_names(spec: GroupSpec) -> Dict[str, str]:
    """group address -> member / reserved host / reservation name"""
    names = {m.ip: m.name for m in spec.members if m.ip}
    names.update({r.ip: r.name for r in spec.reservations})
    names.update({h.ip: h.hostname for h in spec.dhcp_hosts if h.hostname})
    if spec.ipv6_prefix():  # dual stack: their paired IPv6 addresses too
        names.update({spec.ip6_of(ip): name for ip, name in list(names.items())})
    return names
