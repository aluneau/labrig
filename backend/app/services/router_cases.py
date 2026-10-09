"""Router primitives for common customer cases (docs/router-cases.md), rendered for the "el" router

- split DNS: router.dns.zones (dnsmasq server=/zone/ip) + resolver knobs (stop_rebind, no_negcache, cache_size)
- proxy-only egress: router.egress.mode "proxy" = the "blocked" forward rules + squid on the router
- MTU: spec.network.mtu (bridge, router LAN, DHCP option 26) and router.path (narrow hop on the router,
  PMTUD black hole = dropped ICMP frag-needed, TCP MSS clamping)

Pure functions of a normalized GroupSpec, called by router_service.ELRouterBackend: dnsmasq lines,
nft rules / chains, extra pushed files, a shell snippet for the apply command; plus the members'
cloud-init additions (proxy environment) used by group_service.
"""
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from app.schemas.group import ZONE_SERVER, GroupSpec

SQUID_CONF = "/etc/squid/squid.conf"
SQUID_USERS = "/etc/squid/vmm-users"      # user:password (pushed, 0600 after apply)
SQUID_PASSWD = "/etc/squid/vmm-passwd"    # user:apr1-hash, read by basic_ncsa_auth
UPLINK_MTU_MARK = "/var/lib/vmm-router/uplink-mtu"
DEFAULT_MTU = 1500


# Split DNS

def _host_ip(spec: GroupSpec, name: str) -> Optional[str]:
    for m in spec.members:
        if m.name == name:
            return m.ip
    for r in spec.reservations:
        if r.name == name:
            return r.ip
    for h in spec.dhcp_hosts:
        if h.hostname == name:
            return h.ip
    return None


def zone_server(spec: GroupSpec, server: str) -> Optional[str]:
    """'10.0.0.53', '10.0.0.53#5353' or a member name -> dnsmasq server address (ip[#port])"""
    if ZONE_SERVER.match(server):
        return server
    return _host_ip(spec, server)


def dnsmasq_lines(spec: GroupSpec) -> List[str]:
    """Conditional forwarding + resolver knobs (appended to the group's dnsmasq config)"""
    dns = spec.router.dns
    lines: List[str] = []
    if dns.zones:
        lines += ["", "# split DNS: zones forwarded to their own servers"]
        for z in dns.zones:
            for s in z.servers:
                addr = zone_server(spec, s)
                if addr:
                    lines.append(f"server=/{z.domain}/{addr}" + (f"  # {s}" if addr != s else ""))
    if dns.stop_rebind:
        lines += ["", "# DNS rebinding protection: private addresses in upstream answers are dropped",
                  "stop-dns-rebind", "rebind-localhost-ok"]
        lines += [f"rebind-domain-ok=/{z.domain}/" for z in dns.zones if z.allow_private]
    if dns.no_negcache:
        lines.append("no-negcache")
    if dns.cache_size is not None:
        lines.append(f"cache-size={dns.cache_size}")
    if spec.network.mtu:
        lines += ["", "# group network MTU (DHCP option 26)", f"dhcp-option=option:mtu,{spec.network.mtu}"]
    return lines


# MTU

def lan_mtu(spec: GroupSpec) -> int:
    """MTU of the router's LAN interface: the narrow hop's when set, else the group network's"""
    net = spec.network.mtu or DEFAULT_MTU
    return min(spec.router.path.mtu, net) if spec.router.path.mtu else net


def network_mtu_xml(spec: GroupSpec) -> str:
    """<mtu> element of the group's libvirt network (empty = libvirt default, 1500)"""
    return f"<mtu size='{spec.network.mtu}'/>" if spec.network.mtu else ""


def mtu_apply(spec: GroupSpec) -> str:
    """Set the router's LAN (and uplink, for a narrow hop) MTU live and in NetworkManager's profile.
    The uplink is only touched when router.path.mtu is set, or to put it back after it was."""
    fn = ('vmm_mtu() { for d in /sys/class/net/*; do [ "$(cat $d/address 2>/dev/null)" = "$1" ] || continue;'
          ' i=${d##*/}; ip link set dev "$i" mtu "$2" || return 1;'
          ' c=$(nmcli -g GENERAL.CONNECTION device show "$i" 2>/dev/null);'
          ' [ -z "$c" ] || nmcli connection modify "$c" 802-3-ethernet.mtu "$2" || true; return 0; done; return 1; }')
    cmds = [fn, f"vmm_mtu {spec.router.lan_mac} {lan_mtu(spec)}"]
    if spec.uplink and spec.router.uplink_mac:
        path = spec.router.path.mtu
        if path:
            cmds.append(f"vmm_mtu {spec.router.uplink_mac} {path} && mkdir -p {UPLINK_MTU_MARK.rsplit('/', 1)[0]}"
                        f" && echo {path} > {UPLINK_MTU_MARK}")
        else:
            cmds.append(f"if [ -f {UPLINK_MTU_MARK} ]; then vmm_mtu {spec.router.uplink_mac} {DEFAULT_MTU}"
                        f" && rm -f {UPLINK_MTU_MARK}; fi")
    return "{ " + "; ".join(cmds[:1]) + "; " + " && ".join(cmds[1:]) + "; }"


def network_config_mtu(spec: GroupSpec, ethernets: Dict[str, Dict[str, Any]]) -> None:
    """First boot: MTUs in the router's network-config (cloud-init renders them for NetworkManager)"""
    if "lan" in ethernets and lan_mtu(spec) != DEFAULT_MTU:
        ethernets["lan"]["mtu"] = lan_mtu(spec)
    if "uplink" in ethernets and spec.router.path.mtu:
        ethernets["uplink"]["mtu"] = spec.router.path.mtu


# nftables

def nft_forward_rules(spec: GroupSpec) -> str:
    """Rules at the top of the forward chain (before the egress verdicts)"""
    if not spec.router.path.clamp_mss:
        return ""
    # SYNs leaving through an interface with a smaller MTU get their MSS lowered to fit it, both ways
    return ("        tcp flags & (syn | rst) == syn tcp option maxseg size set rt mtu"
            " comment \"vmm mss clamp\"\n")


def nft_output_chain(spec: GroupSpec) -> str:
    """PMTUD black hole: the ICMP 'fragmentation needed' the router generates never leaves it"""
    if not spec.router.path.drop_frag_needed:
        return ""
    return ("    chain output {\n"
            "        type filter hook output priority filter; policy accept;\n"
            "        icmp type destination-unreachable icmp code frag-needed counter drop"
            " comment \"vmm pmtud black hole\"\n"
            "    }\n")


def egress_filtered(spec: GroupSpec) -> bool:
    """The lab's forwarded traffic to the outside is refused (blocked, or proxy only)"""
    return spec.router.egress.mode in ("blocked", "proxy")


# Proxy (squid)

def proxy_enabled(spec: GroupSpec) -> bool:
    return spec.router.egress.mode == "proxy"


def proxy_url(spec: GroupSpec, with_auth: bool = True, host: Optional[str] = None) -> str:
    p = spec.router.egress.proxy
    auth = f"{quote(p.username, safe='')}:{quote(p.password, safe='')}@" if with_auth and p.username else ""
    return f"http://{auth}{host or spec.router.ip}:{p.port}"


def no_proxy(spec: GroupSpec) -> str:
    """Destinations clients must reach directly (the lab, its domain, the router, WireGuard devices)"""
    items = ["localhost", "127.0.0.1", f".{spec.domain}", spec.domain, spec.cidr]
    if spec.ipv6_prefix():  # dual stack (docs/ipv6.md)
        items.append(spec.ipv6_prefix())
    if spec.router.ip:
        items.append(spec.router.ip)
    if spec.router.uplink_ip:
        items.append(spec.router.uplink_ip)
    wg = spec.router.wireguard
    if wg is not None and wg.enabled and wg.subnet:
        items.append(wg.subnet)
    items += [f".{z.domain}" for z in spec.router.dns.zones]
    out: List[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return ",".join(out)


def proxy_env(spec: GroupSpec) -> Dict[str, str]:
    url = proxy_url(spec)
    np = no_proxy(spec)
    return {"http_proxy": url, "https_proxy": url, "HTTP_PROXY": url, "HTTPS_PROXY": url,
            "no_proxy": np, "NO_PROXY": np}


def squid_conf(spec: GroupSpec) -> str:
    p = spec.router.egress.proxy
    lab = [spec.cidr] + ([spec.ipv6_prefix()] if spec.ipv6_prefix() else [])  # squid listens on IPv6 too
    wg = spec.router.wireguard
    if wg is not None and wg.enabled and wg.subnet:
        lab.append(wg.subnet)
    lines = [
        f"# Generated by VM Manager for group '{spec.name}' (egress mode proxy): local changes are overwritten.",
        f"http_port {p.port}",
        f"visible_hostname router.{spec.domain}",
        # the router's dnsmasq: lab names, split DNS zones and the forwarders
        "dns_nameservers 127.0.0.1",
        f"acl lab src {' '.join(lab)}",
        "acl Safe_ports port 80 443 21 1025-65535",
        f"acl SSL_ports port {' '.join(str(x) for x in p.connect_ports)}",
        "acl CONNECT method CONNECT",
    ]
    if p.allow_domains:
        lines.append(f"acl allowed_domains dstdomain {' '.join(p.allow_domains)}")
    if p.username:
        lines += [f"auth_param basic program /usr/lib64/squid/basic_ncsa_auth {SQUID_PASSWD}",
                  "auth_param basic realm VM Manager lab proxy",
                  "acl authenticated proxy_auth REQUIRED"]
    lines += ["http_access deny !Safe_ports", "http_access deny CONNECT !SSL_ports", "http_access deny !lab"]
    if p.allow_domains:
        lines.append("http_access deny !allowed_domains")
    if p.username:
        lines.append("http_access deny !authenticated")
    lines += ["http_access allow lab", "http_access deny all",
              # a proxy, not a cache: small and stateless on a 512 MiB router
              "cache deny all", "cache_mem 8 MB", "memory_pools off",
              "access_log daemon:/var/log/squid/access.log squid",
              "coredump_dir /var/spool/squid", "shutdown_lifetime 2 seconds"]
    return "\n".join(lines) + "\n"


def files(spec: GroupSpec) -> Dict[str, str]:
    if not proxy_enabled(spec):
        return {}
    out = {SQUID_CONF: squid_conf(spec)}
    p = spec.router.egress.proxy
    if p.username:
        out[SQUID_USERS] = f"{p.username}:{p.password}\n"
    return out


def apply_command(spec: GroupSpec) -> str:
    """Shell run after the files are pushed: MTUs always, squid on / off"""
    cmds = [mtu_apply(spec)]
    if not proxy_enabled(spec):
        cmds.append("{ systemctl disable --now squid >/dev/null 2>&1; true; }")
        return " && ".join(cmds)
    p = spec.router.egress.proxy
    squid = ["{ rpm -q squid >/dev/null || dnf -y -q install squid; }"]
    if p.username:
        squid.append(f"chmod 600 {SQUID_USERS} && u=$(cut -d: -f1 {SQUID_USERS})"
                     f" && h=$(cut -d: -f2- {SQUID_USERS} | tr -d '\\n' | openssl passwd -apr1 -stdin)"
                     f" && printf '%s:%s\\n' \"$u\" \"$h\" > {SQUID_PASSWD}.tmp"
                     f" && chown root:squid {SQUID_PASSWD}.tmp && chmod 640 {SQUID_PASSWD}.tmp"
                     f" && mv {SQUID_PASSWD}.tmp {SQUID_PASSWD}")
    if p.port != 3128:
        squid.append(f"{{ semanage port -a -t squid_port_t -p tcp {p.port} 2>/dev/null"
                     f" || semanage port -m -t squid_port_t -p tcp {p.port} 2>/dev/null || true; }}")
    squid += ["{ restorecon -R /etc/squid 2>/dev/null; true; }",
              "squid -k parse -f /etc/squid/squid.conf >/dev/null 2>&1",
              "systemctl enable -q squid",
              "if systemctl is-active -q squid; then systemctl reload squid || systemctl restart squid;"
              " else systemctl restart squid; fi",
              "systemctl is-active squid"]
    return " && ".join(cmds + squid)


def needs_first_boot_apply(spec: GroupSpec) -> bool:
    return proxy_enabled(spec)


# Members (cloud-init)

def member_cloud_config(spec: GroupSpec, config: Dict[str, Any]) -> bool:
    """Add the proxy environment to a member's #cloud-config; False when there is nothing to add"""
    if not proxy_enabled(spec) or not spec.router.egress.proxy.member_env:
        return False
    env = proxy_env(spec)
    url = env["http_proxy"]
    profile = "".join(f"export {k}='{v}'\n" for k, v in env.items())
    environment = "".join(f"{k}={v}\n" for k, v in env.items())
    config.setdefault("write_files", []).extend([
        {"path": "/etc/profile.d/vmm-proxy.sh", "content": "# Lab proxy (VM Manager)\n" + profile,
         "permissions": "0644"},
        {"path": "/etc/environment", "content": environment, "append": True},
    ])
    # Package managers first (bootcmd runs before cloud-init installs packages, on every boot)
    config.setdefault("bootcmd", []).extend([
        f"if [ -d /etc/apt/apt.conf.d ]; then printf 'Acquire::http::Proxy \"{url}\";\\n"
        f"Acquire::https::Proxy \"{url}\";\\n' > /etc/apt/apt.conf.d/90vmm-proxy; fi",
        f"if [ -f /etc/dnf/dnf.conf ] && ! grep -q '^proxy=' /etc/dnf/dnf.conf; then echo 'proxy={url}' >> /etc/dnf/dnf.conf; fi",
    ])
    return True


def summary(spec: GroupSpec) -> Dict[str, Any]:
    """What the UI shows for the proxy (URL, copy-paste environment)"""
    if not proxy_enabled(spec):
        return {}
    return {"url": proxy_url(spec, with_auth=False), "url_with_auth": proxy_url(spec),
            "fqdn_url": proxy_url(spec, with_auth=False, host=f"router.{spec.domain}"),
            "env": proxy_env(spec), "no_proxy": no_proxy(spec)}

