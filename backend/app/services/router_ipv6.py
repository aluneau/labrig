"""IPv6 (dual stack) parts of the "el" router config, kept apart from router_service (docs/ipv6.md)

Everything here is a no-op (or removes what an earlier push added) when the group has no IPv6:
spec.ipv6_prefix() is None. router_service calls these at a few hook points:

    dnsmasq   enable-ra + stateful DHCPv6 range + RA params + DNS / search options, [v6] in dhcp-host,
              v6 in host-record (AAAA)
    nftables  table ip6 vmm_group6: egress blocked (forward) and ipv6.egress drop (prerouting)
    sysctl    net.ipv6.conf.all.forwarding = 1
    NM        the LAN connection gets <prefix>::1/64 (cloud-init network-config at creation, nmcli on
              pushes, so IPv6 can be switched on / off on a running group)
    FRR       address-family ipv6 unicast (dynamic IPv6 neighbors, IPv6 announce ranges, le 128)
    WireGuard IPv6 tunnel address + device /128s
"""
import ipaddress
from typing import List, Optional

from app.schemas.group import GroupSpec

NFT6_TABLE = "vmm_group6"
SYSCTL_FILE = "/etc/sysctl.d/90-vmm-router.conf"
DNSMASQ_IFACE = "/etc/dnsmasq.d/vmm-ipv6-iface.conf"  # written on the router by apply_command
# Dynamic DHCPv6 range: paired addresses (<prefix>::<decimal digits>) never contain hex letters, so a
# range under ::dc:0 can't clash with them
DYNAMIC_START, DYNAMIC_END = 0xdc0000, 0xdcffff
RA_INTERVAL, RA_LIFETIME = 10, 1800


def prefix(spec: GroupSpec) -> Optional[ipaddress.IPv6Network]:
    p = spec.ipv6_prefix()
    return ipaddress.IPv6Network(p) if p else None


def router_ip6(spec: GroupSpec) -> Optional[str]:
    return spec.ip6_of(spec.router.ip)


def lan_address(spec: GroupSpec) -> Optional[str]:
    """The router LAN's IPv6 address with its prefix length (network-config / nmcli)"""
    rip6 = router_ip6(spec)
    return f"{rip6}/64" if rip6 else None


# dnsmasq

def dhcp_addrs(spec: GroupSpec, ipv4: str) -> str:
    """dhcp-host addresses: '10.42.7.21' or '10.42.7.21,[fd00::21]'"""
    ip6 = spec.ip6_of(ipv4)
    return f"{ipv4},[{ip6}]" if ip6 else ipv4


def record_addrs(spec: GroupSpec, ipv4: str) -> str:
    """host-record addresses (A + AAAA)"""
    ip6 = spec.ip6_of(ipv4)
    return f"{ipv4},{ip6}" if ip6 else ipv4


def listen_addresses(spec: GroupSpec) -> List[str]:
    rip6 = router_ip6(spec)
    wg = spec.router.wireguard
    out = [rip6] if rip6 else []
    if rip6 and wg is not None and wg.enabled and wg.subnet6:
        out.append(wg.router_ip6())
    return out


def dnsmasq_lines(spec: GroupSpec) -> List[str]:
    net = prefix(spec)
    if net is None:
        return []
    rip6 = router_ip6(spec)
    start, end = net.network_address + DYNAMIC_START, net.network_address + DYNAMIC_END
    return [
        "",
        "# IPv6 (dual stack): router advertisements (M flag: addresses from DHCPv6, default route via the",
        "# router) + stateful DHCPv6; reserved machines get <prefix>::<host number> (dhcp-host [addr])",
        "enable-ra",
        f"dhcp-range={start},{end},64,12h",
        # MTU in the RAs too when the group sets one (network.mtu, router_cases)
        f"ra-param=*,{f'mtu:{spec.network.mtu},' if spec.network.mtu else ''}{RA_INTERVAL},{RA_LIFETIME}",
        f"dhcp-option=option6:dns-server,[{rip6}]",
        f"dhcp-option=option6:domain-search,{spec.domain}",
    ]


# nftables

def nft_table(spec: GroupSpec, bgp_prefixes: List[str]) -> str:
    """table ip6 vmm_group6, re-created on every push (deleted when the group has no IPv6)"""
    head = f"table ip6 {NFT6_TABLE}\ndelete table ip6 {NFT6_TABLE}\n"
    net = prefix(spec)
    if net is None:
        return head
    v6 = spec.network.ipv6
    wg = spec.router.wireguard
    lab = [str(net)] + ([wg.subnet6] if wg is not None and wg.enabled and wg.subnet6 else [])
    lab += [p for p in bgp_prefixes if ":" in p]
    allow = [a for a in spec.router.egress.allow if ":" in a]
    rules = ""
    if v6.egress == "drop":
        # before routing (the router has no IPv6 route out: without this the kernel would answer "no route"
        # at once): IPv6 from the lab to anything but the lab is dropped silently -> connections hang
        rules += ("    chain prerouting {\n"
                  "        type filter hook prerouting priority filter; policy accept;\n"
                  f"        ip6 saddr {net} ip6 daddr != {{ {', '.join(lab + allow + ['fe80::/10', 'ff00::/8'])} }}"
                  " counter drop comment \"vmm ipv6 egress drop\"\n"
                  "    }\n")
    forward = ""
    if spec.router.egress.mode in ("blocked", "proxy"):  # proxy = blocked + squid (router_cases)
        forward = (f"        ip6 saddr {net} ip6 daddr {{ {', '.join(lab + allow)} }} accept\n"
                   f"        ip6 saddr {net} counter reject with icmpv6 type admin-prohibited"
                   " comment \"vmm egress blocked\"\n")
    rules += ("    chain forward {\n"
              "        type filter hook forward priority filter; policy accept;\n"
              f"{forward}"
              "    }\n")
    return head + f"table ip6 {NFT6_TABLE} {{\n{rules}}}\n"


# Apply (shell, run through the guest agent before dnsmasq restarts)

def apply_command(spec: GroupSpec) -> str:
    """Forwarding + the LAN connection's IPv6 address (NetworkManager), idempotent. With IPv6 off, removes
    an address an earlier push added. Waits until the address is usable (DAD) so dnsmasq finds it."""
    addr = lan_address(spec) or ""
    mac = spec.router.lan_mac
    if not addr:
        # never had IPv6: nothing to do (no nmcli call on every push of every group)
        return (f"{{ dev=$(ip -o link | awk -v m='{mac}' 'tolower($0) ~ m {{sub(\":\", \"\", $2); print $2; exit}}');"
                " if [ -n \"$dev\" ] && ip -6 addr show dev \"$dev\" scope global | grep -q inet6; then"
                " con=$(nmcli -g GENERAL.CONNECTION dev show \"$dev\");"
                " nmcli con mod \"$con\" ipv6.addresses '' ipv6.method disabled && nmcli dev reapply \"$dev\"; fi;"
                f" rm -f {DNSMASQ_IFACE}; true; }}")
    return ("{ grep -q '^net.ipv6.conf.all.forwarding' " + SYSCTL_FILE +
            " || printf 'net.ipv6.conf.all.forwarding = 1\\nnet.ipv6.fib_multipath_hash_policy = 1\\n' >> " + SYSCTL_FILE + "; }"
            " && sysctl -qw net.ipv6.conf.all.forwarding=1 net.ipv6.fib_multipath_hash_policy=1"
            f" && dev=$(ip -o link | awk -v m='{mac}' 'tolower($0) ~ m {{sub(\":\", \"\", $2); print $2; exit}}')"
            " && test -n \"$dev\" && con=$(nmcli -g GENERAL.CONNECTION dev show \"$dev\")"
            f" && if [ \"$(nmcli -g ipv6.addresses con show \"$con\")\" != '{addr}' ]"
            f" || ! ip -6 addr show dev \"$dev\" | grep -q '{addr}'; then"
            f" nmcli con mod \"$con\" ipv6.method manual ipv6.addresses '{addr}'"
            " && { nmcli dev reapply \"$dev\" || nmcli con up \"$con\"; }; fi"
            " && for i in $(seq 40); do ip -6 addr show dev \"$dev\" tentative | grep -q inet6 || break; sleep 0.25; done"
            # dnsmasq answers router solicitations (and sends RAs) only on interfaces named by interface=,
            # listen-address is not enough: name the LAN (found by MAC)
            " && printf '# Generated by VM Manager: the LAN interface (RA + DHCPv6)\\ninterface=%s\\n' \"$dev\""
            f" > {DNSMASQ_IFACE}")


def sysctl_lines(spec: GroupSpec) -> str:
    return ("net.ipv6.conf.all.forwarding = 1\nnet.ipv6.fib_multipath_hash_policy = 1\n"
            if prefix(spec) is not None else "")


# FRR

def frr_prefix_lists(spec: GroupSpec, ranges: List[str]) -> List[str]:
    if prefix(spec) is None:
        return []
    v6 = [r for r in ranges if ":" in r]
    lines = ([f"ipv6 prefix-list VMM-ANNOUNCE6 seq {5 * (i + 1)} permit {r} le 128" for i, r in enumerate(v6)]
             or ["ipv6 prefix-list VMM-ANNOUNCE6 seq 5 deny any"])
    # global next hop (the peer's address on the group network), not its link-local one: readable routes
    return lines + ["!", "route-map VMM-IN6 permit 10", " match ipv6 address prefix-list VMM-ANNOUNCE6",
                    " set ipv6 next-hop prefer-global", "exit", "!"]


def frr_listen(spec: GroupSpec, remote: str) -> List[str]:
    net = prefix(spec)
    if net is None:
        return []
    return [" neighbor LAB6 peer-group", f" neighbor LAB6 remote-as {remote}",
            f" bgp listen range {net} peer-group LAB6"]


def frr_address_family(spec: GroupSpec, peers: List[str], maximum_paths: int) -> List[str]:
    """peers: LAB6 (when listening) + explicit IPv6 neighbors"""
    if prefix(spec) is None:
        return []
    lines = [" !", " address-family ipv6 unicast", f"  maximum-paths {maximum_paths}",
             f"  maximum-paths ibgp {maximum_paths}"]
    for p in peers:
        lines += [f"  neighbor {p} activate", f"  neighbor {p} route-map VMM-IN6 in",
                  f"  neighbor {p} route-map VMM-OUT out"]
    return lines + [" exit-address-family"]


# haproxy

def haproxy_bind(spec: GroupSpec, port: int) -> str:
    """Load balancers listen on IPv6 too (one v4v6 socket) when the group has IPv6"""
    return f"    bind :::{port} v4v6" if prefix(spec) is not None else f"    bind *:{port}"


# WireGuard

def wg_addresses(spec: GroupSpec) -> List[str]:
    wg = spec.router.wireguard
    if prefix(spec) is None or wg is None or not wg.subnet6:
        return []
    return [f"{wg.router_ip6()}/64"]


def wg_peer_ip6(spec: GroupSpec, peer_ip: Optional[str]) -> Optional[str]:
    wg = spec.router.wireguard
    if prefix(spec) is None or wg is None or not wg.subnet6:
        return None
    return wg.ip6_of(peer_ip)
