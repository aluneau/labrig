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

## ACM hub

**Red Hat Advanced Cluster Management (ACM)** manages fleets of clusters from one OpenShift cluster, the
**hub**. Each managed cluster runs an agent, the **klusterlet**, that connects *to the hub's API* (the hub never
needs to reach it afterwards), registers the cluster and runs add-ons (policies, applications, search). In the
console, ACM adds the **All Clusters** perspective (switcher at the top left). Many customers run their clusters
this way, so support cases often start with "the cluster shows Unknown in ACM" or "the import hangs".

### Create a hub

- At creation: *Create cluster* → OpenShift → **Advanced Cluster Management hub** (OpenTofu `acm = true`, API
  `openshift.acm: true`), or the built-in template **ACM hub** (docs/templates.md).
- Day 2: cluster page → **ACM** tab → *Enable ACM* (or *Operators* → ACM hub, or
  `POST /clusters/{id}/openshift/addons {"kind": "acm"}`).

The app installs the `advanced-cluster-management` operator in `open-cluster-management` (its MultiClusterHub
operator subscribes to `multicluster-engine` itself) and a `MultiClusterHub` with `availabilityConfig: Basic`
(one replica per component: Red Hat's choice for single-node hubs), then waits for `status.phase: Running`
(10–20 minutes). The user's pull secret becomes the hub's `imagePullSecret`: non-OpenShift managed clusters pull
the klusterlet from registry.redhat.io with it.

**Size.** Red Hat documents 6 vCPU / 12 GiB reserved for ACM (High availability). The app asks for 12 GiB and
4 vCPU on top of OpenShift's minimum on the nodes that run it, and says what is missing before starting:
SNO **32 GiB / 12 vCPU** (the default when ACM is ticked), compact 3 × (20 GiB, 8 vCPU), HA workers 16 GiB.
Day 2, it also compares the nodes' unrequested CPU / memory with ACM's pod requests.

### Import a cluster

Hub page → **ACM** → *Import cluster* → pick a ready cluster of the app (kubeadm, k3s or OpenShift), or
`POST /clusters/{hub}/acm/managed {"cluster_id": N}`. A task on the hub:

1. **Path check**: the klusterlet must reach `api.<hub>.<domain>:6443`, i.e. the haproxy of the hub's router on
   its uplink address. The app tests it with curl from the managed side (its group router, or its k3s node) and
   refuses in plain words when it can't (see the table).
2. **DNS** on the managed side: owned records `api.<hub zone>` and `*.apps.<hub zone>` → the hub router's uplink
   address in the managed cluster's group (owner `acm:<hub>`), or for k3s a CoreDNS server block in the
   `coredns-custom` configmap.
3. On the hub: `Namespace` + `ManagedCluster` (`hubAcceptsClient: true`) + `KlusterletAddonConfig`.
4. **auto-import-secret** with the cluster's kubeconfig when the hub reaches the cluster's API (curl from the
   hub's router): the hub's import controller installs the klusterlet. Otherwise the app applies the hub's
   `<name>-import` secret (`crds.yaml`, then `import.yaml`) on the cluster itself, as the docs' manual import.
5. Waits for the `ManagedClusterJoined` and `ManagedClusterConditionAvailable` conditions (15 min); on a
   timeout the error lists the klusterlet pods (e.g. ImagePullBackOff).

| Managed cluster | Works | Why |
|---|---|---|
| kubeadm / OpenShift in another lab group | yes | routers share the uplink network (`default`); auto-import-secret (the hub reaches `<uplink>:6443`) |
| k3s on the hub router's uplink network (`network: default`) | yes | same L2 as the routers; CoreDNS answers the hub zone |
| k3s on its own NAT network (`vmm-k-<name>`, the default) | **no** (refused) | libvirt rejects connections from one NAT network to the guests of another |
| managed group with egress `blocked` / `proxy` | after adding `<hub uplink>/32` to the group's egress allow list | refused with that hint |

**Detach**: the row's *Detach* (or `DELETE /clusters/{hub}/acm/managed/{name}`) deletes the ManagedCluster: the hub
removes the klusterlet from the cluster, then the app removes its DNS entries. Deleting a managed cluster removes
its ManagedCluster from the hub; deleting the hub removes its DNS entries from the managed side.

### What to look at

- Console → **All Clusters** → Infrastructure → **Clusters**: `local-cluster` (the hub itself) and the imported
  ones, with their status and add-ons.
- `oc get mch -n open-cluster-management`, `oc get managedclusters` (JOINED / AVAILABLE),
  `oc get managedclusteraddons -A`, `oc get klusterletaddonconfig -A`.
- On the managed cluster: `kubectl get pods -n open-cluster-management-agent` (klusterlet registration / work
  agents) and `-n open-cluster-management-agent-addon`; `kubectl get klusterlet -o yaml` (hub connection errors).
- Things to break: stop the managed cluster (Unknown after ~5 min, back when started); remove the hub's DNS
  record from the managed group (the klusterlet can't renew its lease); detach and re-import with the other
  method (`"method": "secret"` / `"manifests"`).

### Spoke machines (BMC) and provisioning a spoke

At customers, the hub also **installs** clusters on bare-metal servers through their BMCs (iDRAC, iLO): metal3 /
Ironic powers a server and boots it from a virtual-media ISO, the assisted installer does the rest (ZTP, GitOps
`ClusterInstance` / `SiteConfig`, or the `ClusterDeployment` + `AgentClusterInstall` + `InfraEnv` +
`BareMetalHost` objects underneath). The app emulates the BMCs:

- **BMC members**: a lab group member with `bmc: true` (empty disk or ISO) gets a Redfish BMC from
  **sushy-tools** running on the host (one process per group, started by the app), listening on the gateway of the
  group's uplink network (e.g. `192.168.122.1`) on a port of `BMC_HOST_PORTS` (8180-8199), HTTP basic auth with
  per-group credentials (`GET /groups/{id}/bmc/credentials`, never in the spec), and only the group's BMC members
  visible (other VMs: 404). Group page → **BMC (Redfish)** tab: each machine's
  `redfish-virtualmedia://192.168.122.1:8180/redfish/v1/Systems/<uuid>` address (copy it into a BareMetalHost's
  `bmc.address`, with `disableCertificateVerification: true`), its boot MAC, and **Add spoke machines** (N empty
  members `spoke-N`, 16 GiB / 8 vCPU / 120 GiB by default, created powered off: the hub powers them).
  API: `GET /groups/{id}/bmc`, `POST /groups/{id}/bmc/spokes {count, memory, vcpu, disk_size}`; OpenTofu
  `member { source = "empty", bmc = true }`.
- The host firewall must let the group machines reach that port: `scripts/setup.sh` adds
  `ufw allow in on virbr+ to any port 8180:8199 proto tcp` (firewalld: the ports in zone `libvirt`).
- The emulator resolves the names under every lab cluster's zone (`api`, `*.apps`) to that cluster's router: it
  downloads the hub's discovery ISO itself when metal3 inserts the virtual media.
- sushy-tools is pip-installed in the app's venv by `setup.sh` (on Python 3.9 / RHEL 9 pip picks 2.1.0, same
  emulator API). Without it the BMC tab says so and the rest of the app works.

**Provision a spoke** (hub page → ACM → *Provision a spoke*, or `POST /clusters/{hub}/acm/spokes {name, machine}`):
single-node OpenShift on a BMC member **of the hub's group** (it resolves and reaches the hub's routes through the
same router). The task on the hub:

1. **Central infrastructure management**, once: `Provisioning` (`provisioningNetwork: Disabled`,
   `watchAllNamespaces: true`: metal3 on a hub installed with platform none) and `AgentServiceConfig` (assisted
   service and image service; volumes on the hub's default StorageClass: LVMS).
2. Records `api`, `api-int`, `*.apps.<spoke>.<domain>` → the machine's reserved IP on the group router.
3. Namespace `<spoke>`: pull secret, BMC secret, `ClusterImageSet` (the hub's release by default),
   `ClusterDeployment` + `AgentClusterInstall` (SNO, user-managed networking, machine network = group CIDR),
   `InfraEnv`, `BareMetalHost` (`bootMode: legacy`, inspection disabled, `automatedCleaningMode: disabled`, label
   `infraenvs.agent-install.openshift.io`), `ManagedCluster` + `KlusterletAddonConfig`.
4. Waits: BareMetalHost registering → provisioned (the ISO inserted, the machine powered on), the agent
   registering, the install (percentage), then the ManagedCluster Available.

The spoke is a **cluster of the app** (Clusters page, label "spoke of <hub>"): `spec.openshift.provisioner = {kind:
acm, hub_id, hub_name, machine}`, kubeconfig (the hub's `<spoke>-admin-kubeconfig`, pointed at a port the group
router forwards to the spoke API, like the hub's own), kubeadmin password, logs and the MCP `oc` tool work as for any
cluster. `DELETE /clusters/{id}` deletes it **through the hub** (ManagedCluster, ClusterDeployment, BareMetalHost) and
powers the machine off; the machine stays for the next spoke. A hub with spokes can't be deleted (delete the spokes
first). The machine VM carries the cluster metadata, so a lost DB adopts the spoke back. OpenTofu:
`vmmanager_acm_spoke {hub_id, name, machine}` (exports `cluster_id`, `kubeconfig`). What to look at: `oc get bmh,infraenv,agent,agentclusterinstall -n <spoke>`,
`oc get bmh -n <spoke> -o yaml` (`status.errorMessage`: BMC unreachable / bad credentials), the emulator log
`DATA_DIR/groups/<group>/bmc-emulator.log`, the machine's console (discovery ISO banner).

**Real bare-metal spokes** (`POST /clusters/{hub}/acm/spokes {name, host_id, net}`, or hub page → ACM →
*Provision a spoke* → pick a *Bare metal:* host): the same ZTP flow on a **physical server** registered as a
BareMetalHost (Bare-metal hosts page), instead of an emulated-BMC VM. Differences from the emulated path:

- The hub may be **bare-metal / groupless** (no lab-group router). The spoke's `api` / `*.apps.<spoke>.<domain>`
  resolve through the **host DNS resolver** (`services/dns_service.py`, served automatically for any groupless
  OpenShift cluster with an `api_ip`); the host reaches the spoke API by the node IP with
  `tls-server-name api.<spoke>.<domain>` (no router-forwarded port). The spoke node's resolver is the host
  resolver first, so it resolves the hub API (`api.<hub>.<domain>`) for its klusterlet.
- The `BareMetalHost` uses the real `redfish-virtualmedia://<bmc>/redfish/v1/Systems/...` address, **UEFI** boot
  mode, inspection disabled, the host's boot-NIC MAC, and a BMC secret built from the iDRAC credentials
  (`baremetal_service`, stored 0600). A **NMStateConfig** carries the node's static network (IP/prefix/gateway/DNS),
  matched to the boot NIC by MAC.
- metal3/Ironic downloads the discovery ISO from the assisted-image-service and **re-serves it to the BMC by IP**
  (`https://<hub-node-ip>:6183/...`), so the real BMC fetches it without needing DNS for the apps route. As a
  belt-and-braces step the app also points the BMC's own resolver at the host DNS (`IPv4.1.DNS1`, Dell iDRAC;
  the originals are restored on delete) so a name-based image URL would also resolve.
- `DELETE /clusters/{id}` deletes the hub objects and **releases** the host (eject media, clear boot override,
  power off, `cluster_name` cleared) — it is **not** wiped; its BMC DNS is restored. Start / stop power the
  physical host through its BMC. The spoke carries `provisioner = {kind: acm, hub_id, host_id, host_name}`.

**Disconnected hubs**: the mirror request includes `advanced-cluster-management` and `multicluster-engine`
(default channels: the ACM version pairs with that MCE version in the 4.20 catalog), and the MultiClusterHub
subscribes MCE from the mirrored catalog (`installer.open-cluster-management.io/mce-subscription-spec`). Imports
into a disconnected hub are not supported yet: the klusterlet images would have to come from the mirror too.

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

ACM hub + an imported kubeadm cluster (`examples/opentofu/acm/`):

```hcl
resource "vmmanager_cluster" "hub" {
  name = "tofu-acm-hub"
  type = "openshift"
  openshift = { topology = "sno", storage = "lvms", acm = true }   # SNO sized 32 GiB / 12 vCPU by default
}
resource "vmmanager_cluster" "kadm" { name = "tofu-acm-kadm", type = "kubeadm", ctlplanes = 1, workers = 1 }
resource "vmmanager_acm_managed_cluster" "kadm" {
  hub_id     = vmmanager_cluster.hub.id
  cluster_id = vmmanager_cluster.kadm.id     # waits for Joined + Available; destroy = detach
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
