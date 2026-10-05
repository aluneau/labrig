# VM Manager

Web UI + REST API to manage KVM VMs through libvirt, plus an OpenTofu provider. Runs **directly on the
host** (not in a container) against `qemu:///system`.

Goal: labs to reproduce customer cases, on a desktop Linux host used for other things too (no reboot,
libvirt NOT enabled at boot) **and** on RHEL lab machines with the same software, shipped to other people.
Roadmap and designs: `future-features.md` (lab groups with a router VM, then Kubernetes/OpenShift).

## Run

On this rig the app is installed as the `vm-manager` systemd service (`setup.sh --no-boot`: not enabled
at boot, neither is libvirtd). After backend changes: `sudo systemctl restart vm-manager`.
After frontend changes: rebuild (`frontend/build` is served by the backend).

```bash
scripts/setup.sh [--no-boot] [--no-service] [--listen ADDR --port N]   # install / update a host (idempotent)
scripts/package.sh                        # dist/vm-manager-<ver>.tar.gz, UI prebuilt (targets need no Node)
./run.sh                                  # without the service: API + built UI on http://127.0.0.1:8000
cd frontend && npm run build              # rebuild the UI the backend serves (frontend/build)
./run.sh --dev && (cd frontend && npm start)   # dev: backend reload on :8000, UI on :3000 (proxies /api, incl. websockets)
```

The process must be in the `libvirt` group (polkit rule), otherwise every libvirt call fails with
"authentication cancelled". A shell opened before `usermod -aG libvirt` doesn't have the group:
start the server from a fresh login, e.g. `sudo su - $USER -c 'cd …/backend && venv/bin/uvicorn app.main:app --port 8000'`.

Old podman containers `vm-manager-backend/frontend` (stale code, ports 8000/3000) may exist; they are stopped. Don't use them.

## Layout

```
backend/app/
  main.py             FastAPI app: lifespan (DB init, idle-close/state watcher), libvirtError -> 400/404, LibvirtUnavailable -> 503, serves frontend/build
  config.py           Settings (env or backend/.env): LIBVIRT_URI, DEFAULT_POOL_*, DEFAULT_NETWORK, VNC_LISTEN, paths
  database.py         SQLite (WAL, one connection per session), @serialized lock for libvirt->DB mirroring
  libvirt_client.py   ALL libvirt calls; event loop thread + lifecycle callbacks -> event_bus; domain XML template
  events.py           thread-safe EventBus -> asyncio queues (SSE)
  services/           daemon (libvirt start/stop via systemd), helper (pkexec helper), vm, storage (pools/volumes/ISOs/downloads), cloud_image (+ cloud-init seed ISO), network, task, host,
                      group (lab groups), router (RouterBackend.render(spec) -> cloud-init + live files; flavour "el"),
                      wireguard_service (keys, client configs, relay reconcile) + wireguard_relay (UDP relay thread),
                      bgp_service (announce ranges, vtysh status), topology_service (GET /groups/{id}/topology),
                      cluster (k3s, kubeadm; cluster_drivers per type, cluster_network = Libvirt / Group node network)
  api/v1/endpoints/   vms (+ WebSocket /vms/{id}/vnc bridge), storage, networks, hosts, tasks, events (SSE), groups, clusters
  schemas/ models/    Pydantic API schemas / SQLAlchemy models
frontend/src/
  services/api.ts     typed API client (+ vncUrl)       types/index.ts  API types (keep in sync with backend schemas)
  hooks/              useEvents (one EventSource, useLiveEvents), usePolling, useVmPower (pending states)
  pages/              Dashboard, VMs, Console (noVNC), Storage, Networks, NetworkDetail, Groups, GroupDetail, Clusters, ClusterDetail, Host, Tasks
  components/         common/, layout/, vms/CreateVMModal + VmDevices, groups/CreateGroupModal, clusters/CreateClusterModal, console/VncConsole
docs/sriov.md         SR-IOV labs (igb emulation, vIOMMU, VF pools, OpenShift operator settings)
docs/openshift.md     OpenShift (agent-based installer): topologies, add-ons, MetalLB L2 lab, reaching the console
docs/wireguard.md     lab remote access: enable, devices, laptop steps (nmcli import), troubleshooting
docs/bgp.md           BGP on the group router (FRR), MetalLB BGP mode, beginner-friendly
opentofu_provider/    Go provider (terraform-plugin-framework): vmmanager_cloud_image, _network, _vm, _disk, _nic, _group, _wireguard_peer, _cluster
examples/opentofu/    lab (network with DHCP reservations + 2 Debian VMs), devices (disk, ISO, boot order), group (lab group), k3s, kubeadm (clusters)
e2e/                  Playwright browser tests against the real app (see below)
```

## Architecture rules

- **libvirt is the source of truth.** The DB mirrors VMs/pools/volumes/networks only to give them stable
  integer IDs + metadata. Mirroring (`sync_*`, `list_volumes`) runs under `@serialized`, since parallel
  requests would otherwise insert duplicates. Never delete user-visible data automatically (cloud images
  whose volume vanished become `missing`, they are not deleted). Exception: groups / clusters **adopted** from
  libvirt metadata (`adopted` = rebuilt, e.g. created by another instance such as an agent's dev backend) are
  forgotten once nothing of them is left in libvirt: their spec lived there, nothing is lost.
- **Live updates**: libvirt events (domain lifecycle/reboot, network, pool) and task progress →
  `event_bus.publish` → `GET /api/v1/events` (SSE). Frontend subscribes with `useLiveEvents([...kinds])`;
  polling is only a slow safety net. DHCP leases have no events (NetworkDetail polls every 10 s).
- **VNC**: VM consoles listen on 127.0.0.1 (`VNC_LISTEN`); the browser connects through the
  `/api/v1/vms/{id}/vnc` WebSocket bridge. noVNC sends scancodes, so the guest keyboard layout matters
  (cloud-init `keyboard` option, defaults to the browser locale). Scaling is ours (`consoleScale` in
  VncConsole hooks noVNC's `Display.autoscale/_rescale`): nearest-neighbour + snap to whole device pixels
  whenever not downscaling, smooth only when shrinking; noVNC's fractional smooth fit dimmed 1-px console
  glyphs by ~20%. "1:1" toolbar toggle (remembered per browser). The Linux tty itself is grey #AAAAAA on black.
- New VMs: q35, host-passthrough, virtio, no `<emulator>` (libvirt picks it), disks in the `default`
  pool (`/var/lib/libvirt/images`, created if missing). Cloud-image VMs get a full copy of the image
  plus `<name>-cidata.iso` (NoCloud seed, built with pycdlib); delete with `delete_disks` removes both.
- **libvirt on demand** (desktop hosts): no keepalive. `libvirt_client.connect()` opens the connection
  when a request needs it (raises `LibvirtUnavailable` -> 503 `libvirt is stopped` if no socket); the
  lifespan watcher closes it after `LIBVIRT_IDLE_TIMEOUT` min when no SSE client/task, and publishes
  `{"kind":"connection","event":"state"}` from `systemctl show` (never connect to probe: that
  socket-activates the daemon). Event callbacks are deregistered before close (they hold connection refs).
  Start/stop = `systemctl start/stop` as the app user, allowed by `scripts/polkit/50-vm-manager.rules`.
- Privileged operations go through `scripts/vm-manager-helper` (installed root-owned in /usr/libexec,
  run with pkexec, polkit action `org.vmmanager.helper`). Fixed whitelist, validates everything itself.
  Never give the app sudo.
- Background jobs (downloads) use `task_service.start(...)`; task bodies must poll `is_cancelled()`.
- **Devices** (`domain_xml.py` = pure XML helpers, calls in `libvirt_client`, endpoints `vm_devices.py`
  mounted before `vms` so `POST /vms/{id}/disks` isn't a power action): new VMs get an empty SATA CD-ROM
  `sda` (seed moves to `sdb`) and 16 `pcie-root-port`s (hot-plug needs free ports; older VMs fall back
  to "attached at next start"). "User CD-ROM" = first non-`-cidata.iso` CD-ROM. Media changes use
  separate LIVE and CONFIG `updateDeviceFlags` calls (running and saved XML can differ). Boot order keeps
  the XML's style (`<os><boot dev>` or per-device `<boot order>`, never mixed), switching to per-device
  when several CD-ROMs exist. One-shot boot = `vms.next_boot` in the DB, applied by `vm_service._start`
  (define with CD first, start, redefine original). Hot-unplug waits for the DEVICE_REMOVED event (15 s),
  else reports `pending`. VM delete with disks takes disks from both live and saved XML.
- DB schema: `database.init_db()` creates tables and adds missing **nullable** columns (no migration
  tool); new columns must be nullable or have a server default.
- **NICs** are identified by MAC (`/vms/{id}/nics/{mac}`), same live+config pattern as disks (hot-plug, unplug
  waits for DEVICE_REMOVED, else pending); link state / network changes use `updateDeviceFlags`. A NIC on a
  hostdev ("SR-IOV VF pool") network runs as `<interface type='hostdev'>` (live XML has no source network:
  read it from the saved config; `vf: true`). Debian/Ubuntu cloud-image VMs get a network-config with DHCP on
  every NIC (primary by MAC keeps the default route; EL's NetworkManager does it by itself).
- **vIOMMU** = `<iommu model='intel'>` + `<ioapic driver='qemu'/>` (domain_xml.set_iommu), saved config only:
  applies at the next cold start. Host SR-IOV state is read from sysfs (`sriov_service`); VF counts go through
  the helper (`sriov-set-numvfs`). This rig has no IOMMU: test VF pools nested (docs/sriov.md).
- Network settings edits redefine the XML (keeping uuid/bridge/mac/hosts) and restart the network;
  DHCP reservations use `net.update` (live, no restart).
- **Lab groups**: the `GroupSpec` (schemas/group.py) is the source of truth; `normalize()` assigns router
  IP/MACs, DHCP range, member IPs/MACs and stores them in the spec. Network `vmm-g-<name>` is isolated with
  no `<ip>` (no libvirt dnsmasq); router `<name>-rtr` (eth0 uplink, eth1 LAN, matched by MAC in the seed's
  network-config), members `<name>-<member>` (hostname = member name). libvirt objects carry
  `<metadata><vmm:group name role member/>`; the router's also holds the full spec, so `sync_groups()`
  rebuilds the DB. Deletes only touch objects whose metadata names the group. Spec changes are pushed with
  guest-file-write + guest-exec (EL qemu-ga is unrestricted by a systemd drop-in, its SELinux domain made
  permissive). Router readiness = `/var/lib/vmm-router/ready` + dnsmasq active. `vlans` and flavour
  `vyos` are in the schema but rejected by the backends ("not supported yet").
  `spec.dhcp_hosts` = static reservations of non-member machines (`dhcp-host=` + `host-record=` lines,
  validated against router/member IPs/MACs/names in `GroupSpec`). Leases are read from the router's
  `/var/lib/dnsmasq/dnsmasq.leases` via guest-exec; releasing one = stop dnsmasq, delete the line, start it.
  Members (`MemberSpec`) have `source`: `cloud_image` (default, `image` + cloud-init) | `iso` (`iso` = volume
  path or name) | `empty`; the last two get no seed, only the MAC reservation + DNS name. Both the quick
  "Add member" form and CreateVMModal in group mode (`group` prop, or its "Lab group" select) POST a
  MemberSpec to `/groups/{id}/members`; new members are pre-checked (VM name free, <= 64 chars) and dropped
  from the spec again if their VM can't be created.

- **WireGuard remote access** (docs/wireguard.md): `router.wireguard` (`enabled`, `listen_port`; assigned: `subnet`
  from `WG_SUBNET_POOL`, `host_port` from `WG_HOST_PORTS`, router `public_key`; `peers` = devices: public key + tunnel
  IP). The router makes its key pair once (`/etc/wireguard/private.key`, loaded by wg0.conf's `PostUp`, never in the
  spec); the app reads `public.key` back after pushes (`_sync_wg_key`). Peers change live (`wg syncconf`); spec PUTs
  keep the stored peers (`_keep_owned`): devices only come and go through `/groups/{id}/wireguard/peers`. Device key
  pairs generated by the app (pure-Python X25519) are returned once in the client config, never stored. The host
  side is a **UDP relay thread in the app** (`wireguard_relay`, `reconcile(db)` after create/update/delete/pin and at
  startup, DB only), not DNAT: libvirt rejects inbound connections to NAT networks (both firewall backends) and
  rewrites its rules on restart. setup.sh opens `--wg-ports` (default 51820-51869/udp) in ufw/firewalld.
  Client AllowedIPs = group CIDR + tunnel + `uplink_ip/32` (load balancers, kubeadm API) + BGP announce ranges,
  DNS = router tunnel IP.
- **BGP** (docs/bgp.md): `router.bgp` (`asn` 64512, `listen` = dynamic neighbors on the group CIDR with `peer_asn`
  64513 or any, explicit `neighbors`, `announce_ranges` = prefixes accepted `le 32`, assigned a /27 of
  `BGP_ANNOUNCE_POOL` when first enabled, unique on the host). FRR is in every new router's packages (older ones
  `dnf install frr` on the push); `/etc/frr/frr.conf` is a pushed file, `bgpd=yes` sed'ed into `daemons`, applied by
  `systemctl reload frr`. Out route-map denies all; `fib_multipath_hash_policy=1` so ECMP spreads connections; nft
  doesn't masquerade towards announce ranges. Status = `vtysh … json` via guest-exec (`GET /groups/{id}/bgp`). Ranges /
  neighbors owned by `cluster:<name>` survive spec PUTs and block disabling BGP; a bgp block that omits
  `announce_ranges` / `neighbors` keeps the stored ones (OpenTofu sends `{enabled}` only). MetalLB `mode: bgp`
  (OpenShift): pool = owned /27 range, `BGPPeer` to the router LAN IP + `BGPAdvertisement`; switching modes removes the
  other mode's objects, re-creates the demo Service if its IP left the pool and moves `hello.<domain>`.
- **Topology view** (`components/topology/LabTopology.tsx` + `flows.ts`, group Topology tab and OpenShift cluster
  Topology tab): one `GET /groups/{id}/topology` (live WireGuard peers, BGP sessions/routes, MetalLB L2 announcer via
  `oc`), inline SVG laid out per width (laptop/host column, router, L2 bus with machines, virtual IPs; stacked < 820 px),
  PF variables only (dark theme = `pf-v5-theme-dark` class). Every element has `data-key` and a plain-words tooltip;
  "Follow a packet" flows are built from the data (keys of boxes/segments to light up + moving packets).
- **OpenShift** (docs/openshift.md): `openshift_service` (pull secret in `DATA_DIR/openshift/pull-secret.json` 0600,
  never in the DB / API; versions from the upgrade graph API; `openshift-install` + `oc` cached per release in
  `DATA_DIR/openshift/bin/<ver>`, installer cache `XDG_CACHE_HOME=DATA_DIR/openshift/cache`), `openshift_installer`
  (ABI, platform none, always in a lab group: `api`/`api-int`/`*.apps` -> router, haproxy 6443/22623/80/443 => one
  OpenShift cluster per group; install dir `DATA_DIR/openshift/clusters/<name>/`; nodes = empty 120 GiB disk + agent
  ISO, boot disk then CD; progress = Assisted Service on master-0:8090 curled **from the router** (guest-exec), then
  `oc` from the host with `server: https://<uplink_ip>:6443` + `tls-server-name: api.<c>.<d>`; CSRs approved by the
  app), `openshift_addons` (OLM installs, LVMS on the disk with serial `vmm-storage`, ODF, SR-IOV in dev mode on igb
  NICs of the isolated network `vmm-s-<cluster>`, MetalLB L2 + `hello` demo). Group spec `address_pools` keep the
  MetalLB pool out of DHCP / static IPs. Every router serves NTP (chrony `allow <cidr>` + DHCP option): the
  installer validates node clocks. The SR-IOV policy must be created only after the config daemon reported the
  NICs (the controller skips nodes with empty status and doesn't retry).
- **Clusters**: `cluster_service.network_for()` picks the node network: k3s = `LibvirtClusterNetwork`
  (own NAT network `vmm-k-<name>`, no router); kubeadm (`driver.needs_group`) = `GroupClusterNetwork`: the
  nodes are spec `reservations` (static lease + `<name>.<domain>`), DNS records and a `load_balancers` entry
  owned by `cluster:<name>` in the group spec; changes are batched and applied by `commit()`
  (`group_service.update_owned` = save + one router push). User spec edits (`_update`) keep owned entries
  (`_keep_owned`); export and the provider ignore them. Auto-created groups have `spec.owner` and are deleted
  with the cluster (`clusters.group_owned`); a group hosting a cluster can't be deleted. The router's uplink
  lease is pinned as a reservation on the uplink network (`pin_uplink`, `spec.router.uplink_ip`, removed on
  group delete): haproxy listens on every router address, so the kubeconfig points at
  `https://<uplink_ip>:<api_port>` (6443, next free port for a 2nd cluster in a group). kubeadm is
  orchestrated by the app (`driver.bootstrap`): cloud-init only runs `/usr/local/sbin/vmm-k8s-prereqs.sh`
  (containerd.io from Docker's repo, kube* from pkgs.k8s.io, EL SELinux permissive), then guest-exec runs
  `kubeadm init` (v1beta4 config, app token + certificate key, SANs = uplink/router IP + api names),
  Flannel, `kubeadm join` (`--node-name`: EL hostnames are FQDNs; control planes one at a time).

## Portability rules (learned from installing on Arch, Alma 9/10, Debian 13)

- Backend must run on **Python 3.9** (RHEL 9): no `X | None`, no `match`, no `platform.freedesktop_os_release`.
  This rig runs Python 3.14, which evaluates annotations lazily: a class used in an annotation before its
  definition imports fine here but is a NameError on 3.9-3.13. `scripts/check-python.py` catches both.
- Use the distro libvirt bindings (venv with `--system-site-packages`); never require compiling libvirt-python.
- SELinux: systemd can't access files in a home directory, so the unit runs `/bin/sh -c 'cd … && exec venv/bin/python -m uvicorn …'`.
- `ufw`/`firewall-cmd` may be in `/usr/sbin` (not in a user's PATH): probe them through sudo.
- `virsh` output is localized: parse `--name` lists / XML, never human-readable text.
- Downloads: send `Accept-Encoding: identity` and read raw bytes; servers may omit Content-Length (spool fallback exists).
- polkit JS rules work on EL9 (mozjs) and Debian 13 (duktape); `pkexec` is a separate package on Debian.
  libvirt units differ: Arch/Debian monolithic `libvirtd`, EL modular `virtqemud`… (`daemon_service._mode`).
- Test installs in fresh nested VMs created with the app itself (nested KVM is enabled on this host).

## Gotchas

- kubeadm ≥ 1.36 needs containerd 2 (Debian 13 ships 1.7: use Docker's `containerd.io`); EL 10 doesn't
  load `nf_conntrack` early enough for kube-proxy (`modprobe` it). Router memory: 512 MiB holds with the
  cloud-init swap file (dnf at first boot swaps ~60–80 MiB; 384 MiB also boots but swaps ~230 MiB).
- Clusters talk to nodes only through the QEMU guest agent (guest-exec): no SSH key, no route needed.
  EL's qemu-ga forbids guest-exec and is SELinux-confined: cluster nodes get `/etc/sysconfig/qemu-ga`
  with empty filters + a CIL module making `virt_qemu_ga_t` permissive. Right after a restart the
  Kubernetes API still shows the pre-shutdown `Ready`: compare `lastHeartbeatTime` with the boot time.
- SQLAlchemy flushes INSERTs before DELETEs: `flush()` after deleting mirrored rows, or a name reused by
  a re-created VM hits `UNIQUE(name)`.

- noVNC must stay at **1.4.x** (`@novnc/novnc/lib/rfb`): 1.5+ uses top-level await, which CRA can't build.
- `npm` needs `--legacy-peer-deps`. Build with `CI=true npx react-scripts build` to catch lint warnings.
- SSE responses need `Cache-Control: no-transform`, otherwise the CRA dev proxy buffers them.
- ufw: VMs need `ufw allow in on virbr+ to any port 67 proto udp`, `… port 53`, `ufw route allow in/out on virbr+`
  (already set on this host), or they boot without an IP.
- The host locale is French: libvirt error messages come back in French.
- libvirt's `test:///default` driver is handy for quick backend checks
  (`LIBVIRT_URI=test:///default DATABASE_URL=sqlite:////tmp/x.db`), but it lacks volume resize/upload and some undefine flags.

## Verify changes

```bash
cd backend && venv/bin/python -c "import app.main"            # backend imports
scripts/check-python.py                                         # Python 3.9 grammar + annotations used before definition
cd frontend && npx tsc --noEmit -p . && CI=true npx react-scripts build
cd e2e && npm install && node smoke.js                          # every page: console errors, failed requests, screenshots
node lifecycle.js | devices.js | nics.js | full.js | netedit.js | iso.js | kbd.js      # create/console/power/delete, networks, DHCP, downloads, AZERTY
node console.js                                                 # console fidelity: virsh screenshot vs canvas vs page, several viewports/DPRs
node devices.js                                                 # disks hot-add/resize/detach (checked over SSH), ISO, boot once
KUBECTL=… node kubeadm.js                                       # kubeadm in an auto-created group: LB endpoint, kubectl commands, service, stop/start, delete (IMAGE, CTLPLANES=3 HA=1)
KUBECTL=… node clusters.js                                      # k3s: create, host kubectl, copy-paste kubectl commands in bash + fish, stop/start, delete
node groups.js                                                  # lab group: create, in-guest IP/DNS/internet checks, live record, stop/start, delete
node group-dhcp.js                                              # group reservations: make static from a lease, edit, conflicts, release
CLIENT_SH="ssh client" node wireguard.js                        # remote access: device config imported with nmcli on a client VM (not this host)
node bgp.js                                                     # BGP: FRR members, ECMP, filter, WireGuard client VM (created), Topology tab shots, failover
node libvirtctl.js       # STOPS libvirt: only against a nested install (ssh -L tunnel), never this host
cd opentofu_provider && make install && cd ../examples/opentofu/lab && tofu init && tofu apply
```

e2e tests need the server on `BASE_URL` (default http://localhost:8000), Chrome at `CHROME_PATH`, and a
ready Debian 13 cloud image. They create and delete real VMs/networks named `e2e-*`. Screenshots go to
`e2e/screenshots/`. Never touch VMs that aren't named `e2e-*`/`tofu-*` in tests: the user runs their own VMs here.
