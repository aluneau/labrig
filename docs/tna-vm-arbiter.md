# Two-Node + Arbiter (TNA) spoke with the arbiter as a VM — design & feasibility

**Status: DESIGN / BLOCKED on a host-side L2 bridge onto the spoke network (backlog item 6).**
No code is wired for this yet — this document resolves the networking question and records the
implementation plan so it can be built on top of the bridge/VPN feature.

## Goal

PR #11 added multi-node bare-metal spokes, including **TNA** (Two-Node + Arbiter): 2 control-plane
`BareMetalHost`s + 1 arbiter `BareMetalHost`, installed as one `AgentClusterInstall`
(`provisionRequirements: {controlPlaneAgents: 2, arbiterAgents: 1}`). TNA is the GA two-node topology
in OpenShift 4.20: the arbiter holds an etcd replica for quorum, runs no `kube-apiserver` /
`kube-controller-manager` and no workloads, and is sized small (≈2 vCPU / 8 GiB).

The arbiter being a lightweight, workload-free quorum node makes it the natural candidate to run as a
**libvirt VM created by the app** instead of a third physical server. Red Hat explicitly supports the
arbiter as a VM on any RHEL hypervisor including KVM (see References). A true 2-control-plane bare-metal
spoke would then need only the **2 Dells + 1 VM arbiter** — no 3rd physical host.

## The hard question: what network can the arbiter VM be on?

The arbiter is a real cluster node. It must:

1. have a node IP the hub's assisted-service accepts as part of the spoke's `machineNetwork`;
2. have bidirectional connectivity (etcd TCP, OVN-Kubernetes Geneve UDP 6081, kube-apiserver) to the two
   control-plane nodes and the hub;
3. stay within the TNA latency budget (<500 ms end-to-end; trivial on a LAN).

The two control-plane nodes are physical Dells on the lab machine network **`10.6.117.0/24`** (reached
through the routed lab). The arbiter VM runs on the **vm-manager host, `10.6.76.203/24`** — a *different*
L2 segment. So the VM has to be made a member of the spoke's network somehow. Three options were
evaluated against **this lab**, with the facts verified on the running host (2026-10-13):

### Lab facts (verified, read-only)

| Fact | Result |
|------|--------|
| Host NICs | `eno1np0 = 10.6.76.203/24` (primary, default route via `10.6.76.254`); `eno2np1` UP, cabled, **no IP** (host's own DHCP request goes unanswered); `ens5f*` down |
| Host NIC on `10.6.117.0/24`? | **No.** `arping -I eno2np1 10.6.117.254` and `10.6.117.10` → **0 responses**: `eno2np1` is not on the spoke L2 |
| Host → spoke subnet | `ping 10.6.117.254` works (routed L3 via the default gateway) |
| Spoke → host | spoke nodes already use `10.6.76.203` as their DNS resolver → **L3 routing works both ways** between `10.6.76.0/24` and `10.6.117.0/24` |
| libvirt networks on host | `default` (NAT, virbr0 192.168.122.0/24), `bgp-fabric` (isolated), plus a down `br-lab` bridge with no uplink and no members |
| Live clusters (must stay untouched) | hub `sno`/dell-01 (`10.6.117.10`), spoke `spoke2`/dell-02 (`10.6.117.11`, a **SNO**, not a TNA) |

### Option (a) — libvirt bridged/macvtap network on a host NIC onto the spoke L2

Clean and standard: a libvirt network `<forward mode='bridge'><interface dev='…'/></forward>` (macvtap) or
a Linux bridge on the NIC that sits on `10.6.117.0/24` would land the arbiter VM directly on the spoke's
L2 with a real `10.6.117.x` address. The app already knows how to put a VM on a named libvirt network
(`vm_service.create_vm(network_name=…)`), so wiring would be small.

**Not feasible here:** the host has **no NIC on `10.6.117.0/24`** (arping confirmed `eno2np1` is on a
different, quiet segment). Without a physical foot on the spoke L2 there is nothing to bridge/macvtap onto.

### Option (b) — bridge/route the host onto the spoke network (backlog item 6)

Extend the spoke's L2 to the host so the arbiter VM can hold a real `10.6.117.x` address: a Linux bridge
fed by an L2 tunnel (VXLAN/GRE/L2 WireGuard) that terminates on a device already on the `10.6.117`
segment (e.g. one of the Dells, or a lab bridge). This is a **superset of backlog item 6** ("easy VPN
access to a bare-metal-hosted cluster; the host plays the router role"). Item 6 as scoped gives a *laptop*
L3 access into the cluster network via the host; a VM *node* needs the host to present an L2/attachable
address *inside* `10.6.117.0/24`, which is the same machinery taken one step further (an actual L2
extension, not just a routed AllowedIPs tunnel).

**This is the clean, standard, single-`machineNetwork` path — and it depends on building the bridge.**

### Option (c) — cross-subnet (stretched) arbiter: VM on `10.6.76.x`, `machineNetwork` = both /24s

Put the arbiter VM on the host's own LAN (`10.6.76.0/24`, which the host *does* have a NIC on) via a
macvtap/bridge network, give it a real `10.6.76.x` address, and set the spoke's
`machineNetwork: [10.6.117.0/24, 10.6.76.0/24]`. L3 routing between the two subnets works both ways
(verified), and the TNA docs frame the arbiter's requirement as *connectivity + latency, not L2/subnet
adjacency*, so this is not obviously forbidden. The api/ingress VIPs stay on `10.6.117` among the two
masters (the arbiter never holds a VIP), so the on-cluster keepalived LB is unaffected.

**Rejected for now — this is "half-built untestable networking":**

- **Unverifiable in this lab.** Both Dells are committed (dell-01 = hub, dell-02 = spoke2 SNO); there is
  no third/spare host to stand up a real TNA, and a destructive live test is explicitly out of scope. A
  stretched-subnet arbiter is non-standard enough that it must be proven on real hardware before shipping,
  and it cannot be proven here.
- **macvtap vs. host DNS.** A macvtap guest cannot talk to its own host. The spoke flow points node DNS at
  the host resolver (`10.6.76.203`); the arbiter would lose that and need a different resolver — a special
  case just for this node.
- **Shared-lab IP claim.** A bridged VM on `10.6.76.0/24` needs a static address picked by hand in a dense
  shared lab (the same veto risk called out for the bare-metal node IPs), now on the host's *own* segment.
- **Validation risk.** Multiple `machineNetwork` CIDRs with the baremetal on-cluster-LB + a brand-new
  arbiter role is exactly the combination most likely to trip an assisted-service validation, and there is
  no way to find out here without a live install.

## Verdict

**A VM arbiter is not cleanly feasible in this lab without the host-side bridge onto the spoke network
(backlog item 6, extended to an L2 attachment).** The arbiter-as-VM concept itself is sound and
Red-Hat-supported; the blocker is purely that the vm-manager host has only *routed L3* reach to the
spoke's `10.6.117.0/24` and **no L2 presence** there, so the VM cannot hold a spoke-network address.
Per the task's guardrail, implementation stops here rather than forcing option (c)'s untestable
stretched-subnet networking. Build this feature **on top of item 6**, using the plan below.

## Implementation plan (once the L2 bridge exists)

The arbiter would be a cluster node like the others, but a libvirt VM instead of a `BareMetalHost`. The
existing TNA manifest machinery in `acm_spokes._spoke_manifests` already emits
`provisionRequirements.arbiterAgents` and per-node `bmac…/role: arbiter`; only the *arbiter node's*
delivery changes.

1. **Request shape** — in `provision` / `_provision_real_hosts`, let an arbiter node be declared as a VM
   instead of carrying a `host_id`, e.g. a node entry `{"role": "arbiter", "vm": {"vcpu", "memory",
   "disk_gb"}}`. `validate_roles` is unchanged (still exactly 2 masters + 1 arbiter for TNA); only the
   *source* of the arbiter agent differs. Keep the 2 control-plane nodes as real `BareMetalHost`s.

2. **Network** — the arbiter VM's NIC goes on the libvirt bridge network that item 6 creates onto the
   spoke L2 (`10.6.117.0/24`); the VM gets a static spoke IP (one address from the same `spoke_net`), so
   `machineNetwork` stays a single CIDR and the install is a normal single-subnet TNA.

3. **Discovery ISO** — the arbiter has no BMC, so it can't be metal3-driven. Instead boot it off the hub's
   **InfraEnv discovery ISO** directly: read `InfraEnv.status.isoDownloadURL`, download it to the libvirt
   `default` pool (new helper; `openshift_installer._create_image` already shows the upload-to-pool
   pattern), and `vm_service.create_vm(disk_size=…, iso_path=<pool ISO>, network_name=<bridge net>,
   mac_address=<fixed MAC>, start=True)`. The agent self-registers with the hub's assisted-service over
   the network (no `BareMetalHost`/`NMStateConfig` CR for this node — static networking is baked into the
   VM via the NIC on the spoke bridge, or an NMStateConfig that the InfraEnv applies to the agent by MAC).

4. **Bind as arbiter** — the self-registered `Agent` gets `role: arbiter` and the `vmm-spoke=<name>`
   label / cluster binding so the `AgentClusterInstall` consumes it as the arbiter. `_monitor_real` already
   waits for *N* agents; count the VM arbiter in `expected`.

5. **Delete / cleanup** — `release()` deletes the VM (`vm_service.delete_vm(delete_disks=True)`) alongside
   the existing BMH/secret/NMStateConfig cleanup, and removes the arbiter's spoke-network address
   reservation and the downloaded ISO volume.

6. **Model/state** — record the arbiter VM in `spoke_nodes` (role `arbiter`, a `vm` marker instead of a
   `host_id`) so `_nodes`, `summary`, resume and delete all treat it uniformly.

7. **Docs/UI** — extend the spoke-create form so the TNA arbiter can be "a VM on this host" vs. "a third
   bare-metal host"; document the host-bridge prerequisite.

## References

- OpenShift 4.20 — *Installing a Two-Node OpenShift Cluster* (Two-Node with Arbiter): the arbiter may run
  as a VM on a RHEL hypervisor (incl. KVM); requirement is standard node connectivity + <500 ms latency.
  <https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/installing_a_two_node_openshift_cluster/about-two-node-arbiter-installation>
- Backlog item 6 (host-side bridge/VPN onto a bare-metal cluster network) and item 8 (multi-node
  bare-metal spoke, delivered by PR #11).
