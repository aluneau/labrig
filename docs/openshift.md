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
  `redfish-virtualmedia+http://192.168.122.1:8180/redfish/v1/Systems/<uuid>` address (plain HTTP: without `+http`
  Ironic speaks TLS to the emulator and the host stays in "Redfish connection failed"; copy it into a BareMetalHost's
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

**Real bare-metal spokes** (`POST /clusters/{hub}/acm/spokes {name, host_id, net}` for one host, or
`{name, hosts, net, topology}` for several — or hub page → ACM → *Provision a spoke* → pick a topology + *Bare
metal:* host(s)): the same ZTP flow on **physical server(s)** registered as BareMetalHosts (Bare-metal hosts page),
instead of an emulated-BMC VM. Differences from the emulated path:

- The hub may be **bare-metal / groupless** *or* a **VM-based OpenShift cluster in a lab group** (see "VM-based hub"
  below). The spoke's `api` / `*.apps.<spoke>.<domain>` resolve through the **host DNS resolver**
  (`services/dns_service.py`, served automatically for any groupless OpenShift cluster with an `api_ip`); the host
  reaches the spoke API by its node IP (SNO) or its API VIP (multi-node) with `tls-server-name api.<spoke>.<domain>`
  (no router-forwarded port).
- The `BareMetalHost` mirrors a customer's SiteConfig `ClusterInstance` node: a Dell-optimised
  `idrac-virtualmedia://<bmc>/redfish/v1/Systems/...` address (generic `redfish-virtualmedia://` on non-Dell BMCs),
  **UEFI** boot, `automatedCleaningMode: metadata`, `rootDeviceHints: {minSizeGigabytes: 400}` (robust vs guessing a
  disk path), the host's boot-NIC MAC and a BMC secret from the iDRAC credentials (`baremetal_service`, 0600). A
  **NMStateConfig** carries the node's static network (nmstate: static IPv4 by MAC, default route table-id 254,
  dns-resolver search + servers). If the hub had the SiteConfig operator + its `ai-cluster/node-templates`, a single
  `ClusterInstance` would be preferred (it expands into these CRs); the app applies the low-level CRs directly
  because this hub ships only the CRD, not the controller/templates.
- metal3/Ironic downloads the discovery ISO from the assisted-image-service and **re-serves it to the BMC by IP**
  (`https://<hub-node-ip>:6183/...`), so the real BMC fetches it without needing DNS for the apps route. As a
  belt-and-braces step the app also points the BMC's own resolver at the host DNS (`IPv4.1.DNS1`, Dell iDRAC;
  restored on delete).
- **Bidirectional DNS** (the spoke's on-node dnsmasq only knows its own zone and otherwise returns NXDOMAIN,
  shadowing the resolver in `resolv.conf`): the spoke gets a **day-0 MachineConfig** (via
  `AgentClusterInstall.manifestsConfigMapRefs`) forwarding the hub's zone to the host resolver (so its klusterlet /
  assisted-installer-controller reach the hub by name), and the hub gets a **CoreDNS forward**
  (`dns.operator/default`) for the spoke's zone to the host resolver (so ACM's push-import reaches the spoke API).
  The single host resolver serves every groupless cluster's zone and — for a VM hub — the hub's own zone pointed at
  its router uplink (so these forwards work the same whether the hub is bare-metal or a VM in a group).
- `DELETE /clusters/{id}` deletes the hub objects and the hub's CoreDNS forward, and **releases** the host (eject
  media, clear boot override, power off, `cluster_name` cleared) — it is **not** wiped; its BMC DNS is restored.
  Start / stop power the physical host through its BMC. The spoke carries
  `provisioner = {kind: acm, hub_id, host_id, host_name}`.
- **GitOps form**: `GET /clusters/{id}/acm/siteconfig` (spoke row → *SiteConfig (GitOps)*) renders the spoke as an
  ACM SiteConfig `ClusterInstance` (+ a BMC Secret stub, password not embedded) with the `ai-cluster/node-templates`
  templateRefs — the manifest a fleet commits to git for the SiteConfig operator. It is committed for reference next
  to the apply-ready manifests (below); this is the same spoke expressed the way customers define theirs.

### VM-based hub (ACM hub as a VM OpenShift cluster)

The hub need not be a bare-metal cluster: it can be the ordinary **VM-based OpenShift cluster in a lab group** (it
has a router + can get a StorageClass via LVM Storage, so the assisted service's volumes are satisfied). Enabling ACM
and provisioning a real bare-metal spoke works the same, with one reachability design to get right — the hub's
metal3/Ironic + assisted-image-service live inside the isolated group network, while the real Dell BMC and the spoke
node sit on a routable lab network:

- **Hub → real BMC** (metal3 drives virtual media): outbound from the group through the **router's NAT/masquerade**
  to the routable BMC network. Works with the group's default (open) egress; a blocked-egress group must allow the
  BMC subnet.
- **Real BMC → the hub's assisted-image-service** (fetch the discovery ISO): the BMC must resolve
  `*.apps.<hub>.<domain>` and reach the hub ingress. For a VM hub those names otherwise resolve only *inside* the
  group, so the **host DNS resolver publishes the VM hub's zone → its router uplink IP** (`dns_service._vm_hub_zones`,
  active once the hub backs ≥1 real spoke); the group router's **haproxy already fronts the hub API `6443` and
  ingress `80/443` on its uplink**. The app points each BMC's own resolver at the host DNS, so the BMC resolves the
  image-service route and reaches it over the uplink. `ensure_cim` also forwards Ironic's image ports
  (`6180/6183`) from the router uplink, as for the emulated-BMC hub.
- **Spoke node → hub** and **hub → spoke API**: the same bidirectional-DNS plumbing as above, now enabled for a VM
  hub too (the host resolver answers the hub's api/apps via the uplink, and the spoke's api via its node IP/VIP).
- **Lab routing prerequisite** (not something the app can configure): the real server's data network must be able to
  reach the hub router's **uplink IP**, and vice-versa — both are on routable lab networks the vm-manager host
  already reaches. If they are on different L2 segments, the lab's router must route between them.

### Multi-node bare-metal spokes (topologies)

A real bare-metal spoke can span **several BareMetalHosts** installed as one cluster (`hosts` + `net.api_vip` +
`net.ingress_vip`). Each host gets its own BMC Secret + `BareMetalHost` + `NMStateConfig` (its static IP); one
`AgentClusterInstall` carries the role counts in `provisionRequirements` and the two VIPs for the on-cluster
keepalived load balancer (SNO keeps user-managed networking — the single node *is* the API, no VIPs). Supported
topologies (`validate_roles` / `topology_name` in `acm_spokes.py`):

| Topology | Hosts | `provisionRequirements` | Notes |
| --- | --- | --- | --- |
| `sno` | 1 master | `controlPlaneAgents: 1` | single node, user-managed networking |
| `tna` | 2 master + 1 arbiter | `controlPlaneAgents: 2, arbiterAgents: 1` | **the GA two-node control plane** in OCP 4.20 (Two-Node + Arbiter); the arbiter is a small etcd-quorum node (2 vCPU / 8 GiB / ~50 GiB) |
| `compact` | 3 master | `controlPlaneAgents: 3` | schedulable control planes, no workers |
| `ha` | 3-5 master + N worker | `controlPlaneAgents: 3-5, workerAgents: N` | classic |

**On "2-node":** a genuine two-node control plane in 4.20 is **TNA** (2 control plane + 1 arbiter = 3 physical
hosts). A true *2-physical-node* cluster (Two-Node OpenShift with **Fencing**, TNF) is **Technology Preview** in 4.20
and is **not expressible through the `AgentClusterInstall` / ACM flow** (it needs the openshift-installer agent-based
flow with `featureSet: TechPreviewNoUpgrade` + pacemaker/Redfish fencing), so it is not offered here; use TNA for a
two-node footprint. Each node needs its own IP; `api_vip`/`ingress_vip` are two free addresses on the machine
network. The spoke carries `provisioner = {kind: acm, hub_id, host_id (first), host_name, nodes: [{host_id, role}]}`;
`DELETE /clusters/{id}` releases every host.

### GitOps / ZTP (deploy a spoke from git)

The realistic ZTP path: a spoke's provisioning manifests live in **git**, and **OpenShift GitOps (Argo CD)** on the
hub syncs them onto the cluster to provision it — instead of the app applying the CRs with `oc`. Three pieces
(`services/gitops_service.py`, `services/git_server_service.py`, hub ACM tab → *Deploy from git (GitOps)*):

- **The repo** (`DATA_DIR/gitops`, `GET /clusters/{id}/acm/gitops`): every GitOps spoke is committed to
  `clusters/<hub>/<spoke>/manifests.yaml` (the exact apply-ready CRs the direct path applies, minus the Secrets:
  `_spoke_manifests` / `_member_manifests` build both from one place, so git == what runs; real hosts also get a
  reference `siteconfig.yaml`, not synced). ClusterImageSets go once per release to `clusters/<hub>/imagesets/`
  (shared by spokes of a release, `Prune=false`). One directory per hub: each hub's Argo CD syncs only its own. No
  index file: **deleting a spoke's directory in git deprovisions it** (Argo CD prunes its objects on the hub; the
  status then lists it under `missing_from_git` until its cluster row is deleted). One commit per provision /
  delete; direct-apply spokes are not in the synced tree (Argo CD's self-heal would fight the app's delete). An
  older `clusters/<spoke>/` layout is moved under its hub on the next write.
- **The in-lab git server** (`git_server_service.py`): the app runs a **Gitea** process (supervised like the BMC
  emulator — data under `DATA_DIR/git`, binary downloaded on first use at the pinned `GITEA_VERSION`, listening on a
  free port of `GIT_HOST_PORTS`, opened in the host firewall by `scripts/setup.sh --git-ports`). The repo is mirrored
  into it (`vmmanager/spokes`, public / anonymous clone) so the hub's Argo CD clones it over the LAN at the host's
  routed address — no repository credentials. The GitOps card exposes its **web UI** (`web_url`, *Open Gitea*), the
  spokes repo page (`repo_web_url`) and the **admin login** (`admin_user` = `vmmanager`; the password is a secret read
  `GET /clusters/{id}/acm/gitops/git-credentials`, stored 0600 in `DATA_DIR/git/state.json`, never logged — `POST` the
  same path rotates it). Browse / push manually from Gitea with those credentials: before every commit the app
  fetches the server and merges what users pushed (its own files win a conflict), and pushes fast-forward only — an
  unmergeable server history is kept as a `backup/<time>` branch, never silently dropped. Gitea is a child of the
  app: it stops with it and `reconcile` starts it again at startup (data and password kept); Argo CD just retries
  meanwhile. `status.spokes` / `commits` are read from the server (what Argo CD sees).
- **Argo CD on the hub**: the `gitops` day-2 add-on (ACM tab → *Install OpenShift GitOps*, or `POST
  /openshift/addons {kind: gitops}`) installs the OpenShift GitOps operator with `install_operator`, waits for the
  default `openshift-gitops` Argo CD instance, and grants its application-controller a ClusterRole over the ACM
  provisioning CRs (`hive`, `extensions.hive`, `agent-install`, `metal3`, `cluster`/`agent.open-cluster-management`,
  `siteconfig`). *Deploy (apply Application)* (`POST /clusters/{id}/acm/gitops/deploy`) pushes the repo and applies an
  Argo CD `Application` `<hub>-spokes` syncing `clusters/<hub>` (directory source, recurse, include
  `{imagesets/*.yaml,*/manifests.yaml}`; auto-sync, prune, self-heal) and starts a sync at once (Argo CD polls git
  every 3 min); the card shows its Synced / Healthy state, the last operation and its message.
  The **Argo CD web console** is the `openshift-gitops-server` Route on the hub (`apps.<hub>`, served by the host DNS
  resolver): `gitops.status` returns it as `argocd_url` (*Open Argo CD*) and a deep-link to the `<hub>-spokes`
  Application as `application.url` (*View in Argo CD*). The links are disabled with a reason until the Route exists.

Then provision a spoke with **Deploy via GitOps** ticked (`acm/spokes` `gitops: true`, on real BareMetalHosts or on
an emulated-BMC member of the hub's group; refused up front without Argo CD on the hub): the app creates the
Namespace + the pull-secret / BMC Secret (never committed to git), commits + pushes the manifests, applies the
Application, waits for Argo CD to sync them onto the hub, and follows the install to Ready — metal3 + the assisted
service do the rest exactly as in the direct path. An app restart during the install re-attaches to it (Argo CD
keeps going on its own). **Deleting** a GitOps spoke removes its directory from git first and waits for Argo CD to
sync that commit (prune), then cleans up what is left with `oc` — the other order would let self-heal re-create
it. The direct-apply path stays the default. The SiteConfig `ClusterInstance` form would
instead need the SiteConfig operator + `ai-*-templates` on the hub to expand into these CRs.

**Understanding a spoke before you build it**: the spoke-create form prefills sensible defaults as you type — the
gateway becomes `.254` of the node's IP, prefix `24`, and the node is built UEFI / `OVNKubernetes` /
`rootDeviceHints minSizeGigabytes 400` with NTP from the host. *Preview the SiteConfig ClusterInstance*
(`GET /clusters/{id}/acm/siteconfig/preview?host_id=…&ip=…&prefix=…&gateway=…&dns=…&name=…`) renders the real
`ClusterInstance` for the chosen host + the params being typed (reusing `acm_spokes.siteconfig_preview`, nothing is
created; empty fields show as placeholders) so a user sees exactly how the spoke is defined. The spoke row's
*SiteConfig (GitOps)* action renders the same for a provisioned spoke (`GET /clusters/{id}/acm/siteconfig`).

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
