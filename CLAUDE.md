# VM Manager

Web UI + REST API to manage KVM VMs through libvirt, plus an OpenTofu provider. Runs **directly on the
host** (not in a container) against `qemu:///system`.

Goal: labs to reproduce customer cases, on an Arch gaming rig (no reboot,
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
                      cluster (k3s; + cluster_drivers per type, cluster_network = pluggable node network)
  api/v1/endpoints/   vms (+ WebSocket /vms/{id}/vnc bridge), storage, networks, hosts, tasks, events (SSE), groups, clusters
  schemas/ models/    Pydantic API schemas / SQLAlchemy models
frontend/src/
  services/api.ts     typed API client (+ vncUrl)       types/index.ts  API types (keep in sync with backend schemas)
  hooks/              useEvents (one EventSource, useLiveEvents), usePolling, useVmPower (pending states)
  pages/              Dashboard, VMs, Console (noVNC), Storage, Networks, NetworkDetail, Groups, GroupDetail, Clusters, ClusterDetail, Host, Tasks
  components/         common/, layout/, vms/CreateVMModal + VmDevices, groups/CreateGroupModal, clusters/CreateClusterModal, console/VncConsole
opentofu_provider/    Go provider (terraform-plugin-framework): vmmanager_cloud_image, _network, _vm, _disk, _group, _cluster
examples/opentofu/    lab (network with DHCP reservations + 2 Debian VMs), devices (disk, ISO, boot order), group (lab group), k3s (cluster)
e2e/                  Playwright browser tests against the real app (see below)
```

## Architecture rules

- **libvirt is the source of truth.** The DB mirrors VMs/pools/volumes/networks only to give them stable
  integer IDs + metadata. Mirroring (`sync_*`, `list_volumes`) runs under `@serialized`, since parallel
  requests would otherwise insert duplicates. Never delete user-visible data automatically (cloud images
  whose volume vanished become `missing`, they are not deleted).
- **Live updates**: libvirt events (domain lifecycle/reboot, network, pool) and task progress →
  `event_bus.publish` → `GET /api/v1/events` (SSE). Frontend subscribes with `useLiveEvents([...kinds])`;
  polling is only a slow safety net. DHCP leases have no events (NetworkDetail polls every 10 s).
- **VNC**: VM consoles listen on 127.0.0.1 (`VNC_LISTEN`); the browser connects through the
  `/api/v1/vms/{id}/vnc` WebSocket bridge. noVNC sends scancodes, so the guest keyboard layout matters
  (cloud-init `keyboard` option, defaults to the browser locale).
- New VMs: q35, host-passthrough, virtio, no `<emulator>` (libvirt picks it), disks in the `default`
  pool (`/var/lib/libvirt/images`, created if missing). Cloud-image VMs get a full copy of the image
  plus `<name>-cidata.iso` (NoCloud seed, built with pycdlib); delete with `delete_disks` removes both.
- **libvirt on demand** (owner's gaming rig): no keepalive. `libvirt_client.connect()` opens the connection
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
- Network settings edits redefine the XML (keeping uuid/bridge/mac/hosts) and restart the network;
  DHCP reservations use `net.update` (live, no restart).
- **Lab groups**: the `GroupSpec` (schemas/group.py) is the source of truth; `normalize()` assigns router
  IP/MACs, DHCP range, member IPs/MACs and stores them in the spec. Network `vmm-g-<name>` is isolated with
  no `<ip>` (no libvirt dnsmasq); router `<name>-rtr` (eth0 uplink, eth1 LAN, matched by MAC in the seed's
  network-config), members `<name>-<member>` (hostname = member name). libvirt objects carry
  `<metadata><vmm:group name role member/>`; the router's also holds the full spec, so `sync_groups()`
  rebuilds the DB. Deletes only touch objects whose metadata names the group. Spec changes are pushed with
  guest-file-write + guest-exec (EL qemu-ga is unrestricted by a systemd drop-in, its SELinux domain made
  permissive). Router readiness = `/var/lib/vmm-router/ready` + dnsmasq active. v2 blocks (bgp, wireguard,
  vlans) and flavour `vyos` are in the schema but rejected by the backends ("not supported yet").

## Portability rules (learned from installing on Arch, Alma 9/10, Debian 13)

- Backend must run on **Python 3.9** (RHEL 9): no `X | None`, no `match`, no `platform.freedesktop_os_release`.
- Use the distro libvirt bindings (venv with `--system-site-packages`); never require compiling libvirt-python.
- SELinux: systemd can't access files in a home directory, so the unit runs `/bin/sh -c 'cd … && exec venv/bin/python -m uvicorn …'`.
- `ufw`/`firewall-cmd` may be in `/usr/sbin` (not in a user's PATH): probe them through sudo.
- `virsh` output is localized: parse `--name` lists / XML, never human-readable text.
- Downloads: send `Accept-Encoding: identity` and read raw bytes; servers may omit Content-Length (spool fallback exists).
- polkit JS rules work on EL9 (mozjs) and Debian 13 (duktape); `pkexec` is a separate package on Debian.
  libvirt units differ: Arch/Debian monolithic `libvirtd`, EL modular `virtqemud`… (`daemon_service._mode`).
- Test installs in fresh nested VMs created with the app itself (nested KVM is enabled on this host).

## Gotchas

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
cd frontend && npx tsc --noEmit -p . && CI=true npx react-scripts build
cd e2e && npm install && node smoke.js                          # every page: console errors, failed requests, screenshots
node lifecycle.js | full.js | netedit.js | iso.js | kbd.js      # create/console/power/delete, networks, DHCP, downloads, AZERTY
node devices.js                                                 # disks hot-add/resize/detach (checked over SSH), ISO, boot once
node groups.js                                                  # lab group: create, in-guest IP/DNS/internet checks, live record, stop/start, delete
node libvirtctl.js       # STOPS libvirt: only against a nested install (ssh -L tunnel), never this host
cd opentofu_provider && make install && cd ../examples/opentofu/lab && tofu init && tofu apply
```

e2e tests need the server on `BASE_URL` (default http://localhost:8000), Chrome at `CHROME_PATH`, and a
ready Debian 13 cloud image. They create and delete real VMs/networks named `e2e-*`. Screenshots go to
`e2e/screenshots/`. Never touch VMs that aren't named `e2e-*`/`tofu-*` in tests: the user runs their own VMs here.
