# IPv6 in lab groups (dual stack)

A lab group can carry IPv6 next to its IPv4 subnet, to reproduce customer cases such as "the app hangs since we
enabled IPv6", AAAA records nobody can reach, IPv6 BGP or firewall rules that only cover IPv4.

```
            uplink (libvirt "default", IPv4 only, NAT)
                 |
             [ router ]  eth1: 10.42.7.1/24 + fd00:564d:4d00:1::1/64
                 |        dnsmasq: DHCPv4, router advertisements, DHCPv6, DNS (A + AAAA)
   --------------+--------------------- vmm-g-<group> -----------------
   web1  10.42.7.10  fd00:564d:4d00:1::10      (DHCPv6 reservation by MAC)
   web2  10.42.7.11  fd00:564d:4d00:1::11
   any   10.42.7.1xx fd00:564d:4d00:1::dc:xxxx (dynamic)
```

## Switching it on

- **UI**: *Create lab group* → *IPv6 (dual stack)*, or on an existing group: *Network & DNS* tab → *IPv6 (dual
  stack)* switch. Applied live (the router gets its address, dnsmasq starts sending router advertisements).
- **API**: `"network": {"ipv6": {"enabled": true}}` in the group spec (`prefix` optional).
- **OpenTofu**: `ipv6 = { enabled = true }` on `vmmanager_group` (outputs `router_ip6`, `member_ip6s`), see
  `examples/opentofu/ipv6`.

```yaml
network:
  ipv6:
    enabled: true
    prefix: fd00:564d:4d00:1::/64   # optional: default = a free /64 of IPV6_ULA_POOL
    egress: reject                  # or drop (see below)
```

`IPV6_ULA_POOL` (server setting, default `fd00:564d:4d00::/48`) is where the app takes every IPv6 /64 it assigns:
group networks, WireGuard tunnels and BGP announce ranges, unique on the host. The prefix is kept when IPv6 is
switched off and on again.

## How addresses are given out

**Stateful DHCPv6 with reservations**, not SLAAC. Every machine the group knows (router, members, cluster nodes,
DHCP reservations) gets the IPv6 address with the *same host number* as its IPv4 address, written with the same
digits: `10.42.7.21` → `<prefix>::21`, the router `.1` → `<prefix>::1`. The router's dnsmasq:

- sends **router advertisements** with the M ("managed") flag: clients get their default route (the router's
  link-local address) and the on-link /64 from the RA, their address from DHCPv6;
- serves **DHCPv6**: `dhcp-host=<mac>,<ipv4>,[<ipv6>],<name>` (dnsmasq finds the MAC of directly connected
  clients from the neighbour table, whatever DUID they send); unknown clients get `<prefix>::dc:0-ffff`;
- answers **AAAA** records: `<member>.<domain>`, `router.<domain>` have both A and AAAA.

Why not SLAAC: SLAAC addresses depend on the guest (EUI-64 on systemd-networkd, stable-privacy on NetworkManager),
so AAAA records and reservations could not be known in advance. DHCPv6 reservations give every OS the same,
predictable address, exactly like the IPv4 ones. Clients get a `/128` (DHCPv6 has no prefix length) and the /64
on-link route from the RA.

Members created by the app get a cloud-init network-config with DHCPv4 + DHCPv6 + RAs: netplan / networkd on
Debian / Ubuntu, NetworkManager (`ipv6.method auto`) on EL. ISO / empty-disk members get the reservation too:
the installed OS only has to accept RAs and run DHCPv6 (most do by default). Members created *before* IPv6 was
switched on usually pick it up at the next RA (networkd and NetworkManager accept RAs by default); if one doesn't,
re-create it.

`DHCP leases` (Network & DNS tab, `GET /groups/{id}/leases`) lists the DHCPv6 leases too (`family: ipv6`, the
client's DUID and IAID; the MAC comes from the reservation, or from a DUID-LL/LLT).

## DNS records

A record can have `a`, `aaaa` or both (`{name: app, a: 10.42.7.80, aaaa: "fd00:bad::80"}`); wildcards too. AAAA
records are served even when the group has no IPv6: a v4-only lab with AAAA records reproduces "the name has an
IPv6 address but there is no IPv6".

## Egress (traffic leaving the lab)

The uplink (libvirt's `default` NAT network) is IPv4 only: **there is no IPv6 internet**, and none is needed for
lab-internal IPv6. Lab machines still have an IPv6 default route (the router), so IPv6 towards anything outside the
lab's IPv6 networks reaches the router, which:

- `egress: reject` (default): answers at once (no route / administratively prohibited): applications fall back to
  IPv4 immediately;
- `egress: drop`: drops the packets silently (nftables `table ip6 vmm_group6`, prerouting): connections **hang**
  until they time out — the classic "AAAA preferred, IPv6 broken" symptom (template `ipv6-dual-stack`).

The router egress switch (`router.egress.mode` `blocked` / `proxy`, docs/disconnected.md, docs/router-cases.md)
applies to IPv6 too: the ip6 forward chain accepts only the lab /64, the WireGuard tunnel /64, the BGP announce
ranges and IPv6 entries of `allow`, and rejects the rest. In `proxy` mode squid listens on the router's IPv6
address too and accepts clients of the lab /64; `no_proxy` includes the /64. Internet over IPv6 (NAT66 / NPTv6 on
the router) would need an uplink with IPv6: not done.

Address selection to keep in mind when reproducing: glibc's default policy table prefers a ULA destination over
IPv4 (`getent ahosts name` lists the AAAA first), but prefers IPv4 for a *global* IPv6 destination when the
client only has a ULA source. A AAAA pointing at an unreachable ULA is therefore tried first; one pointing at
`2001:db8::…` usually isn't.

## WireGuard remote access

With IPv6 on, the tunnel also gets a /64 (`wireguard.subnet6`, router `::1`), each device the IPv6 address with
its IPv4 host number (`10.44.0.2` → `<subnet6>::2`), and the client config routes the lab /64, the tunnel /64 and
the IPv6 announce ranges. Devices added before IPv6 was switched on need their config downloaded again
(Remote access tab) to get the IPv6 address and routes.

## BGP over IPv6 (MP-BGP)

With BGP and IPv6 both on, the router's FRR also has `address-family ipv6 unicast`: dynamic neighbors from the lab
/64 (peer group `LAB6`, same `peer_asn`), explicit IPv6 neighbors, and IPv6 announce ranges (`le 128`). When BGP
and IPv6 are first both on, the group gets one IPv6 announce range (a /64 of `IPV6_ULA_POOL`, named `lab6`) next to
the IPv4 /27. IPv6 sessions run over IPv6 (separate from the IPv4 ones), routes are installed with the peer's global
next hop (`set ipv6 next-hop prefer-global`). The BGP tab lists IPv6 sessions with an *IPv6* tag.

A member peering with the router over IPv6 (FRR):

```
router bgp 64513
 no bgp ebgp-requires-policy
 no bgp default ipv4-unicast
 neighbor <prefix>::1 remote-as 64512
 neighbor <prefix>::1 disable-connected-check
 address-family ipv6 unicast
  network <announce range>::80/128
  neighbor <prefix>::1 activate
 exit-address-family
```

`disable-connected-check` is needed: a DHCPv6 address is a /128, so FRR doesn't see the router as "directly
connected" and otherwise keeps the eBGP session in `Active` ("No path to specified Neighbor"). The same applies to
MetalLB / other speakers on DHCPv6 addresses (use `ebgp-multihop 2` where there is no such knob). With BFD on the
router, IPv6 sessions use multihop BFD: use `neighbor <prefix>::1 ebgp-multihop 2` instead, plus `neighbor
<prefix>::1 bfd profile …` (docs/bgp.md "IPv6").

## Dual stack clusters (kubeadm, OpenShift)

A cluster in an IPv6 group can run **dual stack**: nodes, pods and Services get an IPv4 and an IPv6 address
(IPv4 is the primary family: the kubeconfig, the API advertise address and single stack Services stay IPv4).

- **kubeadm**: `kubeadm.dual_stack` (default: on when the group has IPv6). **OpenShift**: `openshift.dual_stack`
  (default off). `true` with no `group_id`: the auto-created group gets IPv6. UI: *Create cluster* → *IPv6 dual
  stack* switch (shown for a new group or a group with IPv6). OpenTofu: `dual_stack = true` on `vmmanager_cluster`.
  Create time only. `GET /clusters/{id}`: `dual_stack`, `nodes[].ip6`.

```
                     IPv4                     IPv6
nodes                10.43.4.10 (DHCP)        fd00:564d:4d00:1::10 (DHCPv6 reservation, the group /64)
pods (kubeadm)       pod_cidr 10.244.0.0/16   pod_cidr6 fd00:10:244::/56 (a /64 per node)
services (kubeadm)   service_cidr 10.96/16    service_cidr6 fd00:10:96::/112
pods (OpenShift)     10.128.0.0/14 /23        fd01::/48, hostPrefix 64
services (OpenShift) 172.30.0.0/16            fd02::/112
```

The IPv6 in-cluster networks must stay outside the group's IPv6 networks and `IPV6_ULA_POOL` (checked at create;
pods /48–/64, services /108–/120).

**kubeadm**: `podSubnet` / `serviceSubnet` "v4,v6" in the v1beta4 config, kubelet `--node-ip=<v4>,<v6>` (in
`/etc/default/kubelet` or `/etc/sysconfig/kubelet`, written by the prerequisites script), IPv6 forwarding on (kubeadm's
preflight; networkd / NetworkManager keep handling the RAs: the node keeps its default route), Flannel with
`EnableIPv6` + `IPv6Network` (VXLAN over IPv6 between nodes), kube-proxy dual stack. Cluster nodes in an IPv6 group
get the members' network-config (DHCPv4 + DHCPv6 + RAs), with or without dual stack.

**OpenShift** (agent-based installer): `machineNetwork` = group CIDR + group /64, `clusterNetwork` and
`serviceNetwork` with the IPv6 entries above (OVN-Kubernetes). The nodes take their IPv6 address from the group's
DHCPv6 reservations (RHCOS NetworkManager follows the RA's M flag): no static `networkConfig` in agent-config.
`platform: baremetal` (compact / HA): the API / ingress VIPs get IPv6 twins (`api_vip6` / `ingress_vip6`,
`<prefix>::<host number>` of the IPv4 VIP: free with it, outside the dynamic DHCPv6 range); install-config
`apiVIPs` / `ingressVIPs` = `[v4, v6]`; `api` / `api-int` / `*.apps` AAAA -> the IPv6 VIPs; the router's forwards
(uplink :6443 / :80 / :443) stay IPv4 -> IPv4 VIPs.

**DNS, API**: `api` / `api-int` (and `*.apps` for OpenShift) get an AAAA record = the router's IPv6 address, the
nodes' names their own; haproxy listens on IPv4 and IPv6 (`bind :::port v4v6`), backends stay IPv4.

**MetalLB** (`kubeadm.metallb`, OpenShift's MetalLB add-on): the `IPAddressPool` has an IPv6 range too and the demo
`hello` Service is `PreferDualStack` (two ClusterIPs, two external IPs); `hello.<domain>` gets A + AAAA.

- **L2**: the IPv6 range is *paired* with the IPv4 one (`10.43.4.200-.207` → `<prefix>::200-<prefix>::207`): those
  IPv4 addresses are kept out of DHCP, so their IPv6 twins are free too. A node answers neighbor discovery (NDP)
  for the IPv6 address like ARP for the IPv4 one.
- **BGP** (kubeadm and OpenShift, same code: `metallb.dual_stack_pool`): the cluster gets its own IPv6 announce
  range (a /64 of `IPV6_ULA_POOL`, `<cluster>-metallb6`, released when MetalLB goes back to L2 or off), the pool is
  `<range>::10-<range>::ff`; a second `BGPPeer` (`lab-router6`) peers with the router's LAN IPv6 address with
  `ebgpMultiHop` (a DHCPv6 address is a /128: FRR wouldn't see the router as directly connected), the router
  installs the /128 routes with the node's global next hop. With BFD (`metallb.bfd`), `lab-router6` has the same
  `bfdProfile`: multihop BFD sessions, the router's `LAB6` is multihop too (docs/bgp.md "IPv6"). Stored with the
  cluster: `pool6`, `range6`, `service_ip6`.
- The *MetalLB lab* tab adds the IPv6 checks (address from the pool, router `curl -6`), the BGP / BFD checks list
  the IPv6 sessions; the topology shows the IPv6 service address in the virtual IP box (and the IPv6 VIPs of a
  platform baremetal cluster).

Verified with `e2e/kubeadm-ipv6.js` (1 control plane + 1 worker, Debian 13): both InternalIPs, podCIDRs and podIPs
of both families, pod → pod over IPv6 across nodes, the IPv6 ClusterIP, a member resolving `hello` to A + AAAA and
reaching the IPv6 LB address (L2 and BGP) and the API over IPv6. OpenShift 4.20.41 SNO dual stack installed for real
(InternalIP v4 + v6, cluster / service networks of both families, PreferDualStack MetalLB demo with an IPv6 LB address,
console and API answering on the router's IPv6 address). `e2e/openshift-ipv6.js` (REUSE=1 KEEP=1): OpenShift 4.20.41
SNO dual stack with MetalLB BGP + BFD: IPv4 and IPv6 sessions Established, BFD up on both (IPv6 multihop, 600 ms
detection), /128 route via the node's global address, a member and the router reaching the IPv6 LB address, then
L2 (IPv6 range released, NDP) and back to BGP. kubeadm-ipv6.js with BFD on the IPv6 sessions too. Dual stack platform
baremetal (compact): install-config with two-element VIP lists accepted by `openshift-install agent create image`
4.20.41; no full install run yet.

## Topology, API

- `GET /groups/{id}`: `ip6` on the router, members and hosts; `spec.network.ipv6`.
- `GET /groups/{id}/topology`: `ipv6_prefix`, `router.lan_ip6`, `machines[].ip6`, WireGuard `subnet6` /
  `router_ip6` / peer `ip6`, an `ipv6` router badge; tooltips show both addresses.
- `GET /groups/{id}/bgp`: `router_ip6`, `listen_range6`, sessions with `afi`, IPv6 routes.

## Troubleshooting

```
ip -6 addr; ip -6 route                 # a /128 from DHCPv6 + "default via fe80::… proto ra"
getent ahosts web2.<domain>             # AAAA first?
ping -6 router.<domain>; curl -6 …; tracepath -6 …
# router (console / guest agent)
journalctl -u dnsmasq | grep -i 'DHCPv6\|RTR'
cat /var/lib/dnsmasq/dnsmasq.leases     # DHCPv6 leases after the "duid" line
nft list table ip6 vmm_group6
grep -i 'routeradv\|routersol' /proc/net/snmp6
```

- **No IPv6 address, no RA on the member**: the router must have `interface=<lan>` for dnsmasq (written by the push
  in `/etc/dnsmasq.d/vmm-ipv6-iface.conf`): `listen-address` alone serves DHCPv4 but dnsmasq ignores router
  solicitations. *Router* tab → *Apply again*.
- **Member got a dynamic `::dc:…` address**: its MAC isn't reserved (not a member / reservation), or it was
  re-created with another MAC.
- A stale DHCPv6 lease of a re-created machine (new DUID, same address) is not pruned like IPv4 ones: it expires
  after 12 h; restart the member's DHCPv6 client or the router's dnsmasq.

## Not done (next steps)

- Internet over IPv6 (NAT66 / NPTv6) when the uplink network has IPv6; IPv6-only groups (no IPv4).
- k3s dual stack (its own NAT network has no IPv6); IPv6 primary family / IPv6-only clusters.
- IPv6 NTP (`chrony allow`), IPv6 DNS forwarders, IPv6 load balancer backends.
