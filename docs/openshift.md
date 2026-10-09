# OpenShift clusters (agent-based installer)

The app installs OpenShift Container Platform with Red Hat's **agent-based installer** inside a lab
group. The group's router plays the customer's datacenter: DNS, DHCP reservations, and the external
load balancer (haproxy). Topologies:

| Topology | Nodes | Default size | Notes |
|---|---|---|---|
| `sno` | 1 master | 8 vCPU, 24 GiB, 120 GiB | Single-node OpenShift. Storage: LVMS. |
| `compact` | 3 masters (schedulable) | 8 vCPU, 20 GiB, 120 GiB each | ODF possible on the masters. |
| `ha` | 3 masters + N workers (≥ 2) | workers 4 vCPU, 12 GiB | Ingress on the workers. ODF needs ≥ 3 workers. |

ODF adds to each storage node 2 vCPU and 6 GiB with `odf_profile: lab` (default), 8 vCPU and 24 GiB with
`odf_profile: lean` (Red Hat sizing). A compact cluster with ODF lab = 3 × (8 vCPU, 26 GiB). The app refuses a
cluster whose RAM doesn't fit next to the running VMs (no overcommit: an OOM-killed master corrupts etcd).

## Before the first cluster

1. **Pull secret** (console.redhat.com/openshift/install/pull-secret): in the *Create cluster* dialog
   (paste it, or give a host path such as `~/pull-secret.json`), or
   `curl -X PUT http://127.0.0.1:8000/api/v1/openshift/pull-secret -H 'Content-Type: application/json' -d '{"path":"~/pull-secret.json"}'`.
   It is stored in `backend/data/openshift/pull-secret.json` (mode 0600), never in the database and never
   returned by the API. Keep your own copy private too (`chmod 600 ~/pull-secret.json`).
2. **Disk space**: per release ~1 GB of binaries (`openshift-install`, `oc`) and ~1.4 GB of base ISO cache
   in `backend/data/openshift/`; per cluster 120 GiB thin disks (+ storage disks).

## What happens

1. The lab group (created for the cluster unless you pick one) gets the node reservations and records:
   `api` / `api-int.<cluster>.<domain>` and `*.apps.<cluster>.<domain>` -> router LAN IP; haproxy on
   **6443, 22623** (masters) and **80, 443** (ingress nodes). These ports are fixed: **one OpenShift
   cluster per group**, and no kubeadm cluster in the same group.
2. `openshift-install` and `oc` for the chosen release are downloaded (checksums verified) and cached.
3. `install-config.yaml` (platform `none`, OVN-Kubernetes, machine network = group CIDR) and
   `agent-config.yaml` (rendezvous IP = master-0, hosts with MAC / role / root disk `/dev/vda`) are
   rendered into `backend/data/openshift/clusters/<name>/` (kept: copies `*.orig`, `auth/`, logs);
   `openshift-install agent create image` builds the ISO, uploaded to the default pool.
4. The node VMs boot it (boot order disk, then CD: the empty disk falls through to the ISO, the
   installed disk wins afterwards). Progress comes from the Assisted Service on master-0 (polled from the
   router, the host has no route to the group), then from the API (cluster operators). Pending node CSRs
   are approved.
5. The ISO is ejected and deleted, updates are disabled (channel cleared), add-ons are installed.

Typical times on a desktop: ISO 3–5 min (first time per release: + base ISO extraction), SNO install
40–60 min, add-ons 5–15 min.

## Reaching the cluster

- **kubeconfig** (cluster page): `server: https://<router uplink IP>:6443` with
  `tls-server-name: api.<cluster>.<domain>`, so it works from the host and over WireGuard without DNS.
- **Console**: `https://console-openshift-console.apps.<cluster>.<domain>`, user `kubeadmin` (password on
  the cluster page). The name must resolve:
  - from a laptop: enable **Remote access** on the group (docs/wireguard.md); the tunnel's DNS is the router;
  - from the host: `/etc/hosts` lines for the router's uplink IP (the cluster page prints them):
    `console-openshift-console.apps.<c>.<d>`, `oauth-openshift.apps.<c>.<d>` (+ any route you use).
- **SSH** to the nodes as `core` with the key from the cluster page (from the router, or over WireGuard).

## Add-ons

Chosen at creation, or later from the cluster's *Operators* tab:

- **Operators**: any OLM package (curated list at creation, the live catalog afterwards). The app creates
  the Namespace (the package's suggested one), the OperatorGroup and the Subscription (default channel),
  waits for the CSV, then the operator's CR when one is required (NMState, HyperConverged, NFD).
- **LVMS** (`storage: lvms`): an extra disk per node (serial `vmm-storage`, `/dev/disk/by-id/virtio-vmm-storage`),
  `LVMCluster` -> default StorageClass `lvms-vg1`.
- **ODF** (`storage: odf`): Local Storage Operator (`LocalVolumeSet` `localblock`) + ODF `StorageCluster`
  (3 replicas), default StorageClass `ocs-storagecluster-ceph-rbd`. `lab` footprint (default): small requests /
  memory limits on mon / mgr / mds / OSD (the limit also sizes Ceph's caches), no NooBaa (`multiCloudGateway`
  ignored) and no RGW (`cephObjectStores` ignored): block + CephFS, no S3. Not a supported sizing: functional labs.
  `lean` = the `resourceProfile` as is, with object storage.
- **SR-IOV** (`sriov.enabled`): every node gets a vIOMMU and igb NICs (emulated 82576, up to 7 VFs) on the
  group network. The SR-IOV Network Operator runs in dev mode (`DEV_MODE=TRUE`: igb is not in its
  supported NIC list), `SriovOperatorConfig` with `disableDrain` on SNO / compact, a policy
  `igb-<device type>` (resource `openshift.io/igbnetdev`) and a `SriovNetwork` `igb-net` (whereabouts,
  `192.168.50.0/24`) for namespace `sriov-demo`. `vfio-pci` adds `intel_iommu=on iommu=pt` at install.
  See docs/sriov.md for the nested-virtualization details.
- **MetalLB** (`metallb.enabled`, `metallb.mode`): **l2** (default) = a pool of addresses after the group's DHCP
  range is kept free in the group (`address_pools`), `IPAddressPool lab-pool` + `L2Advertisement`; **bgp** = a /27
  outside the group network (an announce range of the router, see [bgp.md](bgp.md)), `BGPPeer` to the router
  (AS 64513 → 64512) + `BGPAdvertisement`. Switch day 2 with the *MetalLB lab* tab's **Switch to BGP/L2 mode**
  (the demo Service gets an address of the new pool and `hello.<domain>` follows; re-download WireGuard configs).

## Scenario: MetalLB L2 lab

`metallb.demo` deploys `hello` (2 httpd pods answering with their pod and node names) behind a
`Service type=LoadBalancer`; the router serves `hello.<group domain>` -> its external IP. The cluster's
*MetalLB lab* tab draws the path and runs the checks:

```
laptop ──WireGuard──► host relay ──► router (DNS hello.<domain>) ──► group L2 segment
                                                                    │  ARP "who has <service IP>?"
                                                                    ▼
                                                     announcing node (speaker) ──► kube-proxy / OVN ──► hello pods
```

Things to try:
- `curl http://hello.<domain>` from the laptop (WireGuard) or `curl http://<ip>` from the router.
- Which node announces: the tab, or `oc get servicel2statuses -n metallb-system`, or the speaker logs
  `oc -n metallb-system logs ds/speaker -c speaker | grep -i announc`.
- **Failover** (multi-node): stop the announcing node from the nodes table; another speaker sends a
  gratuitous ARP and the IP moves (seconds, longer for the dead node's endpoints).
- `externalTrafficPolicy: Local` vs `Cluster`: `oc -n metallb-demo patch svc hello -p '{"spec":{"externalTrafficPolicy":"Local"}}'`;
  only nodes with a hello pod announce, and the source IP is preserved.

BGP mode: every node announces the service IP to the router, which routes it to all of them (ECMP); the tab's
checks show each node's BGP session and the router's next hops; a stopped node's route disappears (≤ 30 s). The
cluster's *Topology* tab follows a packet from the laptop to a pod in either mode.

## Disconnected installs

**Disconnected** in the create dialog (`openshift.disconnected: true`, OpenTofu `disconnected = true`): the group
router runs a mirror registry (`registry.<domain>:8443`, +8 GiB RAM / 4 vCPUs / 250 GiB thin disk), oc-mirror on
the router copies the release, the add-ons' operators (with their dependencies) and the demo image into it, then
the group's internet access is cut before the nodes boot. The cluster's pull secret holds the registry's
credentials only, the release comes through `imageDigestSources`, OperatorHub shows the mirrored catalog only.
First mirror: ~20+ GB, 30–90 min before the nodes are created. Details, day-2 operators and limits:
[disconnected.md](disconnected.md#openshift).

Not yet: OKD, `platform: baremetal` with VIPs,
adding workers after install, upgrades from the app.

## OpenTofu

`examples/opentofu/openshift/` (SNO + LVMS + MetalLB + NMState):

```hcl
resource "vmmanager_openshift_pull_secret" "this" { path = "~/pull-secret.json" } # or content = file(...)
data "vmmanager_openshift_release" "this" { channel = "stable-4.20" }            # .version = latest

resource "vmmanager_cluster" "ocp" {
  name    = "tofu-ocp"
  type    = "openshift"
  version = data.vmmanager_openshift_release.this.version
  openshift = {
    topology  = "sno"        # compact | ha (+ workers)
    storage   = "lvms"       # odf: >= 3 nodes
    operators = ["kubernetes-nmstate-operator"]
    metallb   = true
    metallb_mode = "l2"      # bgp
    sriov     = false
  }
}
```

Outputs: `kubeconfig`, `kubeadmin_password` (sensitive), `console_url`, `api_endpoint`. Unset `openshift` fields are
read back from the server. **In place** on an installed cluster: adding to `operators`, enabling `metallb` /
`metallb_demo`, switching `metallb_mode` (through the add-on API); removing an operator or disabling MetalLB is
refused; anything else (topology, storage, SR-IOV, channel, sizes) recreates the cluster. An existing cluster can be
adopted with `tofu import vmmanager_cluster.ocp <cluster id>`. Applies wait up to 4 h (install + add-ons).



| Symptom | Where to look |
|---|---|
| ISO build fails | `backend/data/openshift/clusters/<name>/create-image.log` and `.openshift_install.log` |
| Stuck at "Booting the nodes" | node console: the agent ISO's login banner lists failed validations; `curl http://<master-0>:8090/...` from the router |
| Install stuck in cluster operators | cluster page (operator messages); `oc --kubeconfig <downloaded> get co` |
| Nodes NotReady after a restart | pending CSRs: the start task approves them; `oc get csr` |
| Console doesn't open | name resolution of `*.apps.<cluster>.<domain>` (WireGuard DNS or `/etc/hosts`) |

Don't stop a cluster during its first 24 hours: the first certificate rotation happens then.
