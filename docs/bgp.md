# BGP in a lab group (beginner-friendly)

## The idea in one minute

Normally the group router only knows the lab network (e.g. `10.42.7.0/24`): to reach `10.42.7.20` it asks
"who has 10.42.7.20?" on the segment (ARP) and the machine answers. **BGP** lets a machine say something
more: *"send traffic for `10.45.0.5` to me"*, even though `10.45.0.5` is not on the lab network. The router
writes that in its routing table (`10.45.0.5 via 10.42.7.20`). When several machines announce the same
address the router keeps all of them and spreads connections over them (**ECMP**); when one stops, its
announcement disappears and traffic goes to the others.

That is exactly what **MetalLB in BGP mode** does for Kubernetes / OpenShift `LoadBalancer` services, and what
many customers run in front of their clusters. In a lab group the router plays the customer's top-of-rack router.

```
laptop ──WireGuard──► router (AS 64512) ──── lab network 10.42.7.0/24 ────┬── node1 (AS 64513) "10.45.0.5 → me"
                      10.45.0.5 via node1, node2 (ECMP)                    └── node2 (AS 64513) "10.45.0.5 → me"
```

The group page has a **Topology** tab that draws this live, explains every box on hover/tap, and has
**Follow a packet**: pick e.g. "Your laptop opens http://hello.lab" and step through DNS → tunnel → router →
BGP route → node → pod. Read it once with BGP and once with MetalLB L2 to see the difference.

## Turn it on

- Group page → **BGP** tab → **Enable BGP on the router** (an older router installs FRR first, ~1 min).
- API: `PUT /api/v1/groups/{id}/bgp {"enabled": true}`; OpenTofu: `bgp = true` on `vmmanager_group`.
- OpenShift: choose **MetalLB → BGP** when creating the cluster, or **Switch to BGP mode** in its MetalLB lab tab:
  the app enables BGP on the router itself.

Defaults (all changeable in the BGP tab): router **AS 64512**, lab machines **AS 64513**, any machine of the
group network may open a session (dynamic neighbors, no per-machine config), and the router **accepts only
addresses of the announce ranges**: the app gives each group a `/27` of `BGP_ANNOUNCE_POOL` (default
`10.45.0.0/16`, unique on the host). It announces nothing back. SELinux stays enforcing.

## Make a machine announce an address

On a member (Debian / AlmaLinux), as root — the BGP tab shows these lines with your values:

```bash
apt-get install -y frr || dnf install -y frr
sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/daemons && systemctl restart frr
ip address add 10.45.0.1/32 dev lo        # the address this machine serves (not persistent)
vtysh -c 'configure terminal' -c 'router bgp 64513' -c 'no bgp ebgp-requires-policy' \
  -c 'neighbor 10.42.7.1 remote-as 64512' -c 'address-family ipv4 unicast' -c 'network 10.45.0.1/32' \
  -c 'end' -c 'write memory'
```

Kubernetes clusters in the group (kubeadm, k3s) can use upstream MetalLB in BGP mode: the BGP tab shows the
`IPAddressPool` + `BGPPeer` + `BGPAdvertisement` to apply.

## Check it

- **BGP tab**: sessions (`Established`), routes with their next hops (`ECMP ×2`).
- API: `GET /api/v1/groups/{id}/bgp` (sessions, routes), `GET /api/v1/groups/{id}/topology` (everything the
  diagram shows).
- On the router console: `vtysh -c 'show bgp summary'`, `vtysh -c 'show ip route bgp'`, `ip route show proto bgp`.
- From a laptop on WireGuard: `curl http://10.45.0.1/`. The client config routes the announce ranges into the
  tunnel (**download the device config again** after adding a range or switching MetalLB to BGP).

## How it is built

- FRR on the EL router (`frr` installed at first boot; older routers get it on the first push). `/etc/frr/frr.conf`
  is rendered from `router.bgp` in the group spec and applied with `systemctl reload frr` (sessions stay up).
- `bgp listen range <group cidr> peer-group LAB` (remote-as `peer_asn`, or any other AS), explicit `neighbors`,
  `no bgp ebgp-requires-policy`, prefix-list `VMM-ANNOUNCE` (`<range> le 32`) in, deny-all out,
  `maximum-paths 8`, `bgp bestpath as-path multipath-relax`, timers 10/30 s.
- `net.ipv4.fib_multipath_hash_policy = 1` on the router: ECMP hashes on ports, so connections from one client
  spread over the nodes. nftables doesn't masquerade traffic to the announce ranges.
- MetalLB BGP (OpenShift): pool = a `/27` announce range owned by `cluster:<name>` in the group spec,
  `BGPPeer` (myASN 64513 → router LAN IP, AS 64512, hold 30 s) + `BGPAdvertisement`; switching modes deletes the
  other mode's objects and re-creates the demo Service so it gets an address of the new pool (DNS `hello.<domain>`
  follows).

## Troubleshooting

| Symptom | Check |
|---|---|
| No session | Machine's FRR running (`vtysh -c 'show bgp summary'` on it), its AS = the router's "their AS", it targets the router's LAN IP. |
| Session up, no route | The prefix must be inside an announce range (`show bgp ipv4 unicast neighbors <ip> received-routes` needs soft-reconfig; simpler: is it in the BGP tab's ranges?). On the machine, the address must exist (FRR's `network` needs it in its table) and `no bgp ebgp-requires-policy` must be set. |
| Route present, laptop can't reach it | Re-download the WireGuard config (AllowedIPs must contain the range); `ip route get <addr>` on the laptop must say `dev wg-…`. |
| Traffic only to one node | Normal for one connection; several connections spread (ECMP). |

## IPv6

With IPv6 on in the group (docs/ipv6.md), FRR also runs `address-family ipv6 unicast`: IPv6 sessions from the lab /64
(peer group `LAB6`), an IPv6 announce range (`lab6`, a /64 of `IPV6_ULA_POOL`, accepted `le 128`). Members peering
over a DHCPv6 address need `neighbor <router>::1 disable-connected-check` (the address is a /128). Example config and
details: docs/ipv6.md.
