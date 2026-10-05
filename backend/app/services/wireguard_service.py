"""WireGuard remote access to lab groups ("road warrior": a laptop joins a lab)

    laptop --udp--> host:<host_port> --relay (wireguard_relay)--> router uplink_ip:<listen_port> (wg0)
           tunnel <subnet>: router .1, devices .2, .3, ...   AllowedIPs = group CIDR + subnet + uplink_ip/32

The spec's router.wireguard block is the source of truth (peers = public keys + assigned tunnel IPs).
The router generates its own key pair at first use (/etc/wireguard/private.key, never leaves it); the app
reads the public key back through the guest agent. Key pairs the app generates for devices are returned
once, in the client config, and not stored.
"""
import base64
import ipaddress
import logging
import os
import socket
from typing import Any, Dict, List, Optional, Tuple

import libvirt
from sqlalchemy.orm import Session

from app.config import settings
from app.libvirt_client import libvirt_client
from app.models import Group
from app.schemas.group import GroupSpec, WireGuardPeer, WireGuardSpec
from app.services.wireguard_relay import relay

logger = logging.getLogger(__name__)

WG_DIR = "/etc/wireguard"
PUBLIC_KEY_FILE = f"{WG_DIR}/public.key"
KEEPALIVE = 25


# Curve25519 (RFC 7748), for device key pairs: no dependency (python-cryptography isn't in every
# distro's base install, wireguard-tools isn't on the host). Speed is irrelevant here.

_P = 2 ** 255 - 19
_A24 = 121665


def _x25519(scalar: bytes, u: bytes) -> bytes:
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k_int = int.from_bytes(bytes(k), "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        bit = (k_int >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % _P, (x2 - z2) % _P
        aa, bb = a * a % _P, b * b % _P
        e = (aa - bb) % _P
        c, d = (x3 + z3) % _P, (x3 - z3) % _P
        da, cb = d * a % _P, c * b % _P
        x3, z3 = (da + cb) ** 2 % _P, x1 * (da - cb) ** 2 % _P
        x2, z2 = aa * bb % _P, e * (aa + _A24 * e) % _P
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def public_key(private_key: str) -> str:
    return base64.b64encode(_x25519(base64.b64decode(private_key), (9).to_bytes(32, "little"))).decode()


def generate_keypair() -> Tuple[str, str]:
    """(private, public), base64 like `wg genkey | wg pubkey`"""
    private = bytearray(os.urandom(32))
    private[0] &= 248
    private[31] = (private[31] & 127) | 64
    private_b64 = base64.b64encode(bytes(private)).decode()
    return private_b64, public_key(private_b64)


# Host side

def default_endpoint_host() -> str:
    """WG_ENDPOINT_HOST, else the host's primary address (the source address of its default route)"""
    if settings.WG_ENDPOINT_HOST.strip():
        return settings.WG_ENDPOINT_HOST.strip()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent: only picks the route
            return s.getsockname()[0]
        except OSError:
            return socket.gethostname()


def host_firewall() -> Optional[str]:
    """Active host firewall that must let WG_HOST_PORTS/udp in (readable without root)"""
    try:
        with open("/etc/ufw/ufw.conf") as f:
            if any(line.strip().upper() == "ENABLED=YES" for line in f):
                return "ufw"
    except OSError:
        pass
    if os.path.exists("/run/firewalld/firewalld.pid") or os.path.exists("/var/run/firewalld.pid"):
        return "firewalld"
    return None


def port_range() -> Tuple[int, int]:
    first, _, last = settings.WG_HOST_PORTS.partition("-")
    first_i = int(first)
    return first_i, int(last or first_i)


def _wg_of(spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return ((spec or {}).get("router") or {}).get("wireguard")


def reconcile(db: Session) -> None:
    """Relay exactly the enabled groups whose router uplink address is known (DB only, no libvirt)"""
    wanted: Dict[int, Tuple[str, int]] = {}
    for group in db.query(Group).all():
        spec = group.spec or {}
        wg, uplink_ip = _wg_of(spec), (spec.get("router") or {}).get("uplink_ip")
        if group.status == "deleting" or not wg or not wg.get("enabled", True) or not uplink_ip:
            continue
        if wg.get("host_port"):
            wanted[int(wg["host_port"])] = (uplink_ip, int(wg.get("listen_port") or 51820))
    relay.apply(wanted, settings.WG_RELAY_LISTEN)


def assign(db: Session, spec: GroupSpec, old: Optional[WireGuardSpec], group_id: Optional[int],
           taken_subnets: List[str]) -> None:
    """Fill in what the app assigns in spec.router.wireguard: tunnel subnet, host port, peer names / IPs.
    taken_subnets: libvirt network subnets (group networks have no <ip>: their CIDRs come from the DB)."""
    wg = spec.router.wireguard
    if wg is None:
        return
    others = [g for g in db.query(Group).all() if g.id != group_id and g.name != spec.name and g.status != "missing"]
    other_wg = [w for w in (_wg_of(g.spec) for g in others) if w]

    if old is not None:
        wg.subnet = wg.subnet or old.subnet
        wg.host_port = wg.host_port or old.host_port
        wg.public_key = old.public_key  # the router's: only ever read back from it

    used = [ipaddress.IPv4Network(spec.cidr)] + [ipaddress.IPv4Network(c) for c in taken_subnets]
    used += [ipaddress.IPv4Network(g.cidr) for g in others]
    used += [ipaddress.IPv4Network(w["subnet"]) for w in other_wg if w.get("subnet")]
    if wg.subnet is None:
        pool = ipaddress.IPv4Network(settings.WG_SUBNET_POOL)
        wg.subnet = next((str(n) for n in pool.subnets(new_prefix=max(24, pool.prefixlen))
                          if not any(n.overlaps(u) for u in used)), None)
        if wg.subnet is None:
            raise ValueError(f"No free tunnel subnet left in WG_SUBNET_POOL {settings.WG_SUBNET_POOL}")
    elif old is None or wg.subnet != old.subnet:
        clash = next((u for u in used[1:] if ipaddress.IPv4Network(wg.subnet).overlaps(u)), None)
        if clash is not None:
            raise ValueError(f"The tunnel subnet {wg.subnet} overlaps {clash}, already used on this host")

    ports = {int(w["host_port"]) for w in other_wg if w.get("host_port")}
    if wg.host_port is None:
        first, last = port_range()
        wg.host_port = next((p for p in range(first, last + 1) if p not in ports), None)
        if wg.host_port is None:
            raise ValueError(f"No free WireGuard host port left in WG_HOST_PORTS {settings.WG_HOST_PORTS}")
    elif wg.host_port in ports:
        raise ValueError(f"Host port {wg.host_port}/udp is already used by another group's WireGuard")

    net = ipaddress.IPv4Network(wg.subnet)
    old_ips = {p.public_key: p.ip for p in (old.peers if old else []) if p.ip}
    taken = {wg.router_ip()} | {p.ip for p in wg.peers if p.ip}
    names = {p.name for p in wg.peers if p.name}
    for i, peer in enumerate(wg.peers):
        if not peer.name:
            n = i + 1
            while f"peer{n}" in names:
                n += 1
            peer.name = f"peer{n}"
            names.add(peer.name)
        if not peer.ip and old_ips.get(peer.public_key) and old_ips[peer.public_key] not in taken:
            peer.ip = old_ips[peer.public_key]
        if not peer.ip:
            free = next((str(h) for h in net.hosts() if str(h) not in taken), None)
            if free is None:
                raise ValueError(f"No free address left in the tunnel subnet {net}")
            peer.ip = free
        taken.add(peer.ip)


def client_allowed_ips(spec: GroupSpec) -> List[str]:
    """Split tunnel: the group network, the tunnel, the router's uplink address (load balancers,
    e.g. a kubeadm API at https://<uplink_ip>:6443) and the networks behind the peers"""
    wg = spec.router.wireguard
    nets = [spec.cidr, wg.subnet]
    if spec.router.uplink_ip:
        nets.append(f"{spec.router.uplink_ip}/32")
    return nets


def client_config(spec: GroupSpec, peer: WireGuardPeer, endpoint_host: str,
                  private_key: Optional[str] = None) -> str:
    wg = spec.router.wireguard
    host = endpoint_host.strip()
    endpoint = f"[{host}]:{wg.host_port}" if ":" in host else f"{host}:{wg.host_port}"
    lines = [
        f"# Lab group {spec.name} ({spec.domain}), device {peer.name}: generated by VM Manager",
        f"# Linux: nmcli connection import type wireguard file {config_filename(spec)}  (or wg-quick up ./{config_filename(spec)})",
        "[Interface]",
        f"PrivateKey = {private_key}" if private_key else
        f"# PrivateKey = <the private key of {peer.public_key}: add it here>",
        f"Address = {peer.ip}/32",
        # wg-quick and NetworkManager treat a non-address DNS entry as a search domain
        f"DNS = {wg.router_ip()}, {spec.domain}",
        "",
        "[Peer]",
        f"# router of {spec.name}",
        f"PublicKey = {wg.public_key}",
        f"Endpoint = {endpoint}",
        f"AllowedIPs = {', '.join(client_allowed_ips(spec))}",
        f"PersistentKeepalive = {KEEPALIVE}",
    ]
    return "\n".join(lines) + "\n"


def config_filename(spec: GroupSpec) -> str:
    """NetworkManager / wg-quick name the interface after the file: <= 15 characters"""
    return f"wg-{spec.name}"[:15].rstrip("-") + ".conf"


# Router side (through the guest agent)

def read_router_key(vm_name: str) -> Optional[str]:
    try:
        result = libvirt_client.agent_exec(vm_name, "/bin/cat", [PUBLIC_KEY_FILE], timeout=10)
    except (libvirt.libvirtError, TimeoutError, KeyError, ValueError) as e:
        logger.info(f"Cannot read the WireGuard public key of {vm_name}: {e}")
        return None
    key = result.get("stdout", "").strip()
    try:
        return key if len(base64.b64decode(key, validate=True)) == 32 else None
    except ValueError:
        return None


def read_peers(vm_name: str) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """public key -> live state from `wg show wg0 dump` (the interface line, which holds the private
    key, is dropped on the router)"""
    script = ("if wg show wg0 >/dev/null 2>&1; then wg show wg0 dump | tail -n +2; "
              "else echo 'wg0 is not up on the router' >&2; exit 3; fi")
    try:
        result = libvirt_client.agent_exec(vm_name, "/bin/sh", ["-c", script], timeout=10)
    except (libvirt.libvirtError, TimeoutError, KeyError, ValueError) as e:
        return {}, f"Cannot read the WireGuard state of the router: {e}"
    if result["exitcode"] != 0:
        return {}, (result.get("stderr") or result.get("stdout") or "").strip() or "wg show failed"
    peers = {}
    for line in result["stdout"].splitlines():
        f = line.split("\t")
        if len(f) >= 8:
            peers[f[0]] = {"endpoint": None if f[2] == "(none)" else f[2], "latest_handshake": int(f[4] or 0),
                           "rx_bytes": int(f[5] or 0), "tx_bytes": int(f[6] or 0)}
    return peers, None
