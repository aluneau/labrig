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

Kubernetes clusters: **kubeadm** clusters get MetalLB installed by the app (next section); for anything else
(k3s, your own MetalLB) the BGP tab shows the `IPAddressPool` + `BGPPeer` (+ `BFDProfile`) + `BGPAdvertisement` to apply.

## MetalLB on kubeadm clusters (installed by the app)

Like OpenShift's MetalLB add-on, for kubeadm clusters (always in a lab group):

- **Create**: Create cluster → kubeadm → MetalLB (mode L2 or BGP, BFD, demo). API:
  `POST /api/v1/clusters {..., "type": "kubeadm", "kubeadm": {"metallb": {"enabled": true, "mode": "bgp", "bfd": true, "demo": true}}}`.
  OpenTofu: `kubeadm = { metallb = true, metallb_mode = "bgp", metallb_bfd = true }` on `vmmanager_cluster`.
- **Day 2**: the cluster's **MetalLB lab** tab (enable, switch L2 ↔ BGP, turn BFD on, remove) or
  `PUT /api/v1/clusters/{id}/metallb {"enabled": true, "mode": "l2"}` (a task; `enabled: false` removes MetalLB and gives
  the pool back). `GET /api/v1/clusters/{id}/metallb` = the lab tab's data (pool, service IP, next hops / announcing node,
  BGP + BFD sessions, live checks). Both endpoints also work for OpenShift (same as its add-on API).
- What the app does: upstream MetalLB **v0.16.1, FRR mode** (`metallb-frr.yaml`: BFD needs FRR), downloaded by the first
  control plane and applied with kubectl through the guest agent; waits for the controller and the speakers; then the
  same configuration as OpenShift (shared code, `services/metallb.py`): pool (L2: a range of the group network kept out
  of DHCP; BGP: a /27 announce range owned by `cluster:<name>`, BGP enabled on the router if needed), `L2Advertisement`
  or `BGPPeer` (AS 64513 → router LAN IP, AS 64512, + `BFDProfile` when the router runs BFD) + `BGPAdvertisement`; the
  `hello` demo (2 replicas behind a LoadBalancer Service) published as `hello.<group domain>` (`hello-<cluster>` when
  another cluster of the group already has `hello`). Switching modes swaps the pool and re-creates the Service.
- kubeadm puts `node.kubernetes.io/exclude-from-external-load-balancers` on control planes: their speaker still opens a
  BGP session but doesn't announce, so the router's next hops are the workers (the lab tab shows exactly that).
- Deleting the cluster removes its pool, announce range and DNS name from the group (owned entries).

## Fast failover with BFD

Without BFD, when a node dies (power cut, kernel panic, `virsh destroy`) nothing closes its BGP session: the router
keeps sending its share of the traffic to the dead node until the **hold time** (30 s) expires. **BFD** (Bidirectional
Forwarding Detection) is a tiny UDP hello (port 3784) both sides send every few hundred milliseconds; after
*multiplier* missed hellos the session is declared down and BGP withdraws the routes at once.

- Turn it on: group **BGP** tab → **BFD** (multiplier 3, 200 ms by default: detection 600 ms), API
  `PUT /groups/{id}/bgp {"bfd": {"enabled": true, "detect_multiplier": 3, "receive_interval": 200, "transmit_interval": 200}}`,
  OpenTofu `bgp_bfd = true`, or MetalLB **BFD** (OpenShift / kubeadm, BGP mode: turns it on on the router too).
- Router side: FRR `bfdd`, one profile `vmm` and `neighbor LAB bfd profile vmm` (and every explicit neighbor).
  Turning BFD on or off **restarts FRR** (sessions re-establish in ~10 s): FRR 8.5's reload mangled the config when
  the `bfd` block came or went. The router also has a static BFD session per known lab address (members, reservations,
  cluster nodes): FRR 10 keeps a `neighbor … bfd` session Idle until BFD is up, and a dynamic peer only got one from
  bgpd after BGP was up (a rebooted member never came back). So adding a lab address restarts FRR when BFD is on.
- Peers must run BFD too: FRR `bfdd=yes` + `neighbor <router> bfd profile …` (the BGP tab's sample shows it), MetalLB a
  `BFDProfile` referenced by the `BGPPeer` (the app does it). The negotiated interval is the slower of both sides.
- Status: the BGP tab's **BFD sessions** table (status, detection time, intervals both ways, last down reason),
  `GET /groups/{id}/bgp` (`bfd_peers`, `sessions[].bfd_status`), the Topology tooltips; on the router
  `vtysh -c 'show bfd peers'`.

Measured on this lab (router = AlmaLinux 9 FRR 8.5.3, members Debian 13 FRR 10.3, MetalLB v0.16.1 speakers; time between
the node's last answer to a 50 ms ping from the router and the router dropping it as next hop, `e2e/bgp-bfd.js` and
`e2e/kubeadm-metallb.js`):

| Failure (force-stopped VM) | Without BFD | With BFD (3 × 200 ms) |
|---|---|---|
| FRR member announcing an anycast /32 | 26.5–27 s (hold time 30 s) | 0.63–0.70 s |
| kubeadm worker, MetalLB BGP speaker | (hold time, up to 30 s) | 0.62 s |
| MetalLB L2 (no BGP): another node takes the IP over | 19 s of failed requests | (n/a) |

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
- MetalLB BGP (OpenShift and kubeadm, `services/metallb.py`): pool = a `/27` announce range owned by `cluster:<name>`
  in the group spec, `BGPPeer` (myASN 64513 → router LAN IP, AS 64512, hold 30 s, `bfdProfile: lab-bfd` when the router
  runs BFD) + `BGPAdvertisement`; switching modes deletes the other mode's objects and re-creates the demo Service so
  it gets an address of the new pool (DNS `hello.<domain>` follows).

## Troubleshooting

| Symptom | Check |
|---|---|
| No session | Machine's FRR running (`vtysh -c 'show bgp summary'` on it), its AS = the router's "their AS", it targets the router's LAN IP. |
| Session up, no route | The prefix must be inside an announce range (`show bgp ipv4 unicast neighbors <ip> received-routes` needs soft-reconfig; simpler: is it in the BGP tab's ranges?). On the machine, the address must exist (FRR's `network` needs it in its table) and `no bgp ebgp-requires-policy` must be set. |
| Route present, laptop can't reach it | Re-download the WireGuard config (AllowedIPs must contain the range); `ip route get <addr>` on the laptop must say `dev wg-…`. |
| Traffic only to one node | Normal for one connection; several connections spread (ECMP). |
| Service down ~30 s after a node dies | No BFD: the router waits for the hold time. Turn BFD on (BGP tab) and give the peers BFD (MetalLB: Re-apply in the MetalLB lab tab). |
| BFD session `down` / missing for a peer | The peer doesn't run BFD (`bfdd=yes`, `neighbor … bfd`), or its BGPPeer has no `bfdProfile`. BFD failing doesn't stop BGP from coming up. |
| Only the workers are next hops (kubeadm) | Expected: control planes carry `exclude-from-external-load-balancers`. |

## IPv6

With IPv6 on in the group (docs/ipv6.md), FRR also runs `address-family ipv6 unicast`: IPv6 sessions from the lab /64
(peer group `LAB6`), an IPv6 announce range (`lab6`, a /64 of `IPV6_ULA_POOL`, accepted `le 128`). Members peering
over a DHCPv6 address need `neighbor <router>::1 disable-connected-check` (the address is a /128). Example config and
details: docs/ipv6.md.
BFD covers the IPv4 sessions only: IPv6 sessions (`LAB6`, IPv6 neighbors) fall back to the BGP hold time.
