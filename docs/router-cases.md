# Router primitives for customer cases

Three settings of a lab group's router that reproduce frequent customer network problems, all applied live
(the router config is re-rendered and pushed through the guest agent, no reboot):

| Case | Spec | UI | Template |
|---|---|---|---|
| Split DNS: internal names only on an internal resolver | `router.dns.zones`, `router.dns.{stop_rebind,no_negcache,cache_size}` | Network & DNS → *Resolver* | `split-dns` |
| Internet only through an HTTP proxy | `router.egress.mode: proxy`, `router.egress.proxy` | Registry & egress → *Proxy-only egress* | `proxy-only-egress` |
| MTU / PMTUD black hole | `network.mtu`, `router.path` | Network & DNS → *MTU and path MTU* | `mtu-1400` |

OpenTofu: `dns_zone` blocks, `egress.proxy`, `mtu`, `path` on `vmmanager_group` (`examples/opentofu/router-cases`).
End-to-end test: `e2e/router-cases.js`.

## Split DNS

```yaml
router:
  dns:
    forwarders: [1.1.1.1]                 # everything else (empty = the uplink's DNS)
    zones:
      - {domain: corp.example, servers: [dns1]}            # a member name, an IPv4 or ip#port
      - {domain: 10.in-addr.arpa, servers: [10.42.7.53]}   # reverse zones work too
    stop_rebind: false   # true: drop private addresses in upstream answers (zones are exempt unless allow_private: false)
    no_negcache: false   # true: don't cache NXDOMAIN
    cache_size: null     # 0 = no cache
```

The router's dnsmasq answers the lab zone (`<group>.lab`) itself, sends each zone to its servers
(`server=/corp.example/10.42.7.53`) and the rest to the forwarders. A server given as a member name is resolved to
the member's address when the config is rendered. The zone can't be the group's own domain; a sub-zone of it
(`ad.<group>.lab`) is fine (more specific wins).

**Internal DNS server pattern**: a member whose `user_data` installs its own resolver, authoritative for the
customer's zone and nothing else (the `split-dns` template uses dnsmasq: `no-resolv`, `local=/corp.example/`,
`host-record=`). Clients use the router; internal names resolve through the conditional forwarding, public ones
through the forwarders, and `dig @1.1.1.1 app.corp.example` shows the public side doesn't know them. Reproduce the
customer's mistake by pointing a client straight at one resolver, or by removing the zone.

`stop_rebind` is dnsmasq's DNS rebinding protection (`stop-dns-rebind`): a public name answered with a private
address is dropped, the classic "the internal app's public name has no address behind this resolver". Zones get
`rebind-domain-ok` unless `allow_private: false`.

## Proxy-only egress

```yaml
router:
  egress:
    mode: proxy            # open | blocked | proxy
    allow: []              # still reachable directly (as with blocked)
    proxy:
      port: 3128
      allow_domains: []    # [".debian.org", "quay.io"]: other destinations get 403 from squid
      connect_ports: [443] # CONNECT only to these ports (an HTTPS service on 6443 / 8443 is refused)
      username: null       # basic auth with password (407 without)
      password: null
      member_env: true     # new cloud-image members get the proxy environment
```

`proxy` = the `blocked` forward rules (direct traffic from the lab to anything but the lab, WireGuard devices, BGP
ranges and `allow` is rejected at once) + **squid** on the router. Why squid and not tinyproxy: squid is in EL's
AppStream (tinyproxy is EPEL only), it does CONNECT, dstdomain allowlists and basic auth (`basic_ncsa_auth`), and its
access log is what customers' proxies look like. It runs as a plain forward proxy with no cache (`cache deny all`,
8 MB memory cache: fine on the 512 MiB router), resolves through the router's dnsmasq (lab names and split-DNS zones
work through the proxy), accepts clients from the group network and WireGuard devices only, and does no TLS
interception (HTTPS stays end to end through CONNECT). It is installed with dnf the first time the mode is used
(~1 min on an existing router) and stopped when the mode changes; its settings are kept across mode switches.

Members created from a cloud image while the mode is on get, through cloud-init: `/etc/profile.d/vmm-proxy.sh` and
`/etc/environment` (`http_proxy`, `https_proxy`, `no_proxy`, and upper case), `/etc/apt/apt.conf.d/90vmm-proxy` /
`proxy=` in `/etc/dnf/dnf.conf` (written by `bootcmd`, so the first-boot package install goes through the proxy).
`NO_PROXY` = `localhost,127.0.0.1,.<domain>,<domain>,<cidr>,<router ip>,<uplink ip>,<WireGuard subnet>,.<zones>`.
Members with raw `user_data` get nothing: add `apt: {proxy: …}` (or dnf `proxy=`) yourself, else their first boot
can't install packages (no guest agent). Untick *member_env* to reproduce an unconfigured host.
The UI shows the proxy URL and the environment to copy (`GET /groups/{id}` → `proxy`).

On the router: `tail -f /var/log/squid/access.log` (`TCP_DENIED/407` = no credentials, `TCP_DENIED/403` = not
allowed, `TCP_TUNNEL/200` = HTTPS CONNECT). The disconnected-lab code (`openshift.disconnected`, registry) is
unchanged: it switches egress to `blocked` and restores the previous mode (`proxy` included) on delete.

## MTU and the PMTUD black hole

```yaml
network:
  mtu: 1500            # the group network: libvirt <mtu>, router LAN, DHCP option 26 (default 1500)
router:
  path:
    mtu: 1400          # the router as a narrow hop: its uplink + LAN interfaces (576-1500, <= network.mtu)
    drop_frag_needed: true   # it drops the ICMP "fragmentation needed" it generates (nft output chain)
    clamp_mss: false   # rewrite forwarded SYNs' MSS to the route MTU (nft `tcp option maxseg size set rt mtu`)
```

Scenarios:

1. **Narrow hop, PMTUD working** (`path.mtu: 1400`): hosts send 1500-byte packets with DF; the router can't forward
   them and answers ICMP type 3 code 4 (mtu 1400); senders lower their path MTU (`ip route get` shows `mtu 1400`).
   `ping -M do -s 1373 <beyond>` fails with "Frag needed and DF set (mtu = 1400)" / "Message too long".
2. **PMTUD black hole** (+ `drop_frag_needed`): the same packets vanish silently. Small exchanges work (DNS, ping
   ≤ 1372 bytes of payload, HTTP headers), anything with full-size segments hangs: downloads, TLS handshakes with big
   certificate chains, `ls` of a big directory over SSH, image pulls. Both directions: the router drops downloads
   on its LAN side and uploads on its uplink side.
3. **The fix on the router**: `clamp_mss` — SYNs crossing the router get MSS = route MTU - 40 (1360), so TCP never
   sends too-big segments; ICMP / UDP stay broken (clamping is TCP only).
4. **The fix on the hosts**: `network.mtu: 1400` (or `ip link set … mtu 1400`): members send small packets.
5. **Jumbo lab** (`network.mtu: 9000`, no path): 9000 inside, 1500 outside: PMTUD needed for anything leaving.

Checks from a member: `ip link`, `ping -c2 -M do -s <mtu-28> <addr>` (28 = IP + ICMP headers), `tracepath -n`,
`ip route get <addr>` (learned MTU; `ip route flush cache` to forget it), `ss -ti` (retransmits, mss), and on the
router `tcpdump -ni any 'icmp[0] == 3 and icmp[1] == 4'`, `nft list ruleset` (the black-hole counter).

How it's applied: the router's interface MTUs are set live (`ip link`) and in NetworkManager's profile (found by
MAC), and in the first-boot network-config. `network.mtu` reaches members by DHCP: new members and reboots get it
at once, running ones at their next DHCP reconfigure (`networkctl reconfigure enp1s0` on Debian, `nmcli device
reapply`): systemd-networkd doesn't apply a new MTU on a plain lease renewal. The libvirt network's `<mtu>` is
rewritten in its saved definition: the bridge and the VMs' taps take it at the next group start (lowering works
live anyway; raising above 1500 needs that restart).
