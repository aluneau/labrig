# Handoff: state on 2026-10-05 (late evening, end of session)

Read `CLAUDE.md` first (layout, run/verify commands, architecture rules, portability rules), then
`future-features.md` ("Status at a glance", designs, §3.3 OpenShift plan, §4 next steps). This file covers
**where we are and what's next**.

## What this session added (all on `main`, service restarted, verified)

| Feature | State | Docs |
|---|---|---|
| **WireGuard remote access** to a lab group | Router runs wg0; the app relays host UDP `51820-51869` → router (no root, no DNAT: libvirt's NAT rules reject inbound). Remote access tab: devices, QR code, `.conf` download, copy-paste commands to connect / clean up the laptop (NetworkManager, wg-quick, a one-line base64 import that works in bash/zsh/fish). Split tunnel (group CIDR + tunnel + router uplink + BGP ranges), router DNS. | `docs/wireguard.md` |
| **OpenShift** (agent-based installer) | SNO / compact / HA in a lab group; router = DNS (`api`, `api-int`, `*.apps`) + haproxy (6443, 22623, 80, 443) ⇒ **one OpenShift cluster per group**. Version picker (upgrade graph API), cached `openshift-install`/`oc`, pull secret stored 0600 (never in DB/API). Install progress from the Assisted Service (curled from the router) then cluster operators; CSR approval; console, kubeadmin, kubeconfig with `tls-server-name`, SSH key. | `docs/openshift.md` |
| **Add-ons** | Any OLM operator (curated list at create, live catalog after), LVMS, ODF (LSO + lean StorageCluster), SR-IOV (igb NICs on an isolated `vmm-s-<cluster>` network, vIOMMU, operator in dev mode, sample policy), MetalLB L2 or BGP + `hello` demo. Day-2 from the Operators tab. | `docs/openshift.md` |
| **BGP** on group routers | FRR, router AS 64512, dynamic neighbors from the group network, only "announce ranges" accepted (a /27 of `10.45.0.0/16` per group), ECMP with port hashing. BGP tab with sessions/routes + copy-paste FRR / upstream MetalLB configs. | `docs/bgp.md` |
| **Topology view** (owner's ask: "self-explanatory") | Topology tab on groups and OpenShift clusters: laptop → host → router → lab network → nodes → virtual IPs, plain-words tooltips, "Follow a packet" stepper, BGP / L2-ARP / NAT / WireGuard / DHCP-DNS explainers. MetalLB lab tab: diagram + live checks + failover demo. | in the UI |
| Routers | Now also serve **NTP** (chrony `allow <cidr>` + DHCP option): the OpenShift installer checks node clocks. Group spec `address_pools` keep MetalLB pools out of DHCP/static IPs. | |
| **OpenTofu** | `vmmanager_group` `wireguard*` + `bgp`; `vmmanager_wireguard_peer`; `vmmanager_cluster` `type = "openshift"` + `openshift {…}` block (read back from the server; in place: add operators, enable MetalLB / demo, switch L2⇄BGP), `console_url`, `kubeadmin_password`, import; `vmmanager_openshift_pull_secret`; data source `vmmanager_openshift_release`. Example `examples/opentofu/openshift`. | `docs/openshift.md` §OpenTofu |
| Small fixes | Unknown `/api/...` paths return a JSON 404 (a newer UI on a not-restarted backend used to show "Unexpected token '<' … not valid JSON"). Clusters rebuilt from libvirt metadata: OpenShift spec comes back from `backend/data/openshift/clusters/<name>/spec.json`, kubeconfig from the install dir. | |

### Verified for real (2026-10-05)
- **OpenShift SNO 4.20.39** (`e2e-ocp`) installed end to end by the app: 34/34 cluster operators, LVMS default
  StorageClass, SR-IOV 4 VFs on igb, NMState, MetalLB L2 then **BGP** (switched day 2): all 6 MetalLB lab checks
  green on :8000, `hello.lab` answered from the router and from a WireGuard client.
- WireGuard end to end in a nested AlmaLinux 9 install with a NetworkManager client VM (nmcli import, DNS,
  split tunnel, disable/enable, device removal, restarts, `kubectl get nodes` over the tunnel, OpenTofu).
- BGP lab (`e2e/bgp.js`): 2 members announcing an anycast /32, ECMP over both, refused out-of-range prefix,
  failover when a member stops, reachability from a WireGuard client.
- OpenTofu: import of the live SNO plans no changes; adding `web-terminal` applied in place (50 s); removing an
  operator is refused; k3s example apply / re-plan / destroy unchanged.
- On :8000 after the restart: `smoke.js` and `groups.js` pass; all OpenShift tabs load without errors.

### Not verified (do these before relying on them)
- **ODF** (needs 3 nodes with ~16 vCPU / 40 GiB each: doesn't fit next to the owner's VMs on this rig — try on a RHEL lab host).
- **Compact / HA OpenShift** installs (same code path, never run), BGP ECMP over several OpenShift nodes,
  creating a cluster directly in `metallb_mode = "bgp"`, the SR-IOV fix on a *fresh* install (on e2e-ocp the
  policy was re-triggered by hand; the code now waits for NIC discovery), `vfio-pci` device type.
- A full `tofu apply` that **creates** an OpenShift cluster (only import + in-place were run), `tofu apply` with
  the group `bgp` attribute.
- WireGuard from outside the LAN (needs a port forward on the home router), Windows/macOS/phone clients,
  the ufw branch of `setup.sh --wg-ports`, an EL10 router with FRR.

## This host (CachyOS gaming rig)

- `vm-manager` service (User=aluneau, 127.0.0.1:8000) and libvirtd are **not enabled at boot**. After a reboot:
  `sudo systemctl start libvirtd vm-manager`. **Restart the service after merging backend changes** (and rebuild
  the frontend): the UI is served from `frontend/build`, so a new UI on an old backend breaks new pages.
- ufw: `51820:51869/udp` opened this session (WireGuard relay), plus the older `virbr+` rules.
- **Still pending owner action:** `scripts/setup.sh --no-boot` (installs the root helper + polkit rules; the Host
  page says "Privileged helper not installed"). Not needed for WireGuard/OpenShift/BGP.
- **Running now** (owner's decision whether to keep them):
  - `e2e-ocp` — test SNO from this session (24 GiB, group `e2e-ocp` with router, MetalLB **BGP** mode,
    `hello.lab` → 10.45.0.1). Delete it from the Clusters page when done (also removes its group, ISO, `vmm-s-e2e-ocp`).
  - `test2` — an OpenShift cluster the **owner** started from the UI this evening (installing at last check). Not ours: don't touch.
  - `test` (kubeadm) and its group — the owner's.
- OpenShift data: `backend/data/openshift/` (pull secret 0600, `bin/4.20.39`, base ISO cache ~1.4 GB,
  `clusters/<name>/` install dirs with kubeadmin password + SSH key). `~/pull-secret.json` is mode 644: suggest `chmod 600`.
- Git: local `main` only, no remote. All agent worktrees/branches merged and removed.

## Known limitations / open issues

- **No authentication** (127.0.0.1 by default). The WireGuard relay listens on 0.0.0.0 (UDP 51820-51869).
- OpenShift: one cluster per group (fixed router ports); no add/remove nodes, upgrades, OKD, disconnected,
  `platform: baremetal` VIPs; the RAM budget check refuses overcommit (by design). Don't stop a cluster in its
  first 24 h (certificate rotation). Operators can't be uninstalled from the app.
- Dev backends on DB copies (how agents test) leave the :8000 DB stale for the objects they touched: the service
  rebuilds clusters from libvirt metadata, but `sync_groups` doesn't refresh an existing group's spec — reload it
  from the router's `<vmm:group>` metadata if a group looks out of date (done for `e2e-ocp` this session).
- BGP: only on the "el" router; announce ranges are per group (/27); no BFD, no route export to the uplink.
- Everything from the previous handoff still applies (no VM CPU/memory edit, groups without uplink, kubeadm
  upgrades, OpenTofu wholesale `dns_record`/`dhcp_host` lists, …).

## How this session worked

Owner asked for agents; pattern from `parallel-agents-workflow` memory: research agent (kcli study) → plan in
`future-features.md` §3.3 → coordinator wrote the OpenShift backend while a UI agent built the pages against the
agreed schemas, and a BGP/topology agent worked in parallel; each in its own worktree, port, DB copy and scratch
dir; one OpenShift install at a time (RAM). A real SNO install surfaced 4 bugs fixed on `main` (SR-IOV NIC on the
group network → address overlap; no NTP for the installer; SR-IOV policy race; demo pods couldn't write their
docroot). Usage limits interrupted agents twice; resuming them with SendMessage worked.

## Next steps (owner's priorities)

1. Owner: try the new UI on `test2` / `e2e-ocp` (laptop over WireGuard: group → Remote access → Add device,
   then the Topology and MetalLB lab tabs), decide whether to keep `e2e-ocp`; run `scripts/setup.sh --no-boot`.
2. Verify on a bigger host (RHEL lab machine): ODF on compact, HA install, BGP ECMP over 3 nodes, `tofu apply`
   creating an OpenShift cluster from scratch, SR-IOV on a fresh install. Add `e2e/openshift.js` (SNO + LVMS +
   MetalLB demo, long) to the suite.
3. OpenShift next: add workers (`oc adm node-image create`), OKD (no pull secret), disconnected (mirror
   registry; needs groups without uplink), `platform: baremetal` VIPs as an option, upgrades.
4. Groups v2: no-uplink groups, VLANs, site-to-site WireGuard between hosts, snapshots/templates.
5. Before sharing widely: authentication, a remote + CI, resource budget for every group/cluster type.

## How to verify after changes

```bash
cd backend && venv/bin/python -c "import app.main" && cd .. && scripts/check-python.py
cd frontend && node node_modules/.bin/tsc --noEmit -p . && CI=true node node_modules/.bin/react-scripts build   # npm isn't installed here
sudo systemctl restart vm-manager
cd e2e && node smoke.js && node groups.js     # + wireguard.js (needs CLIENT_SH, nested host), bgp.js, the rest in CLAUDE.md
cd opentofu_provider && go vet ./... && make install   # then tofu init -upgrade in example dirs (provider checksum changes)
```
