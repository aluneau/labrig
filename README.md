# VM Manager

A web UI and REST API to manage KVM virtual machines through libvirt.

- **VMs**: create from a cloud image (configured with cloud-init), an install ISO, or an empty disk;
  start / shut down / reboot / pause / force off / delete; see IPs and disks. Insert / eject ISOs
  live (also from the console page), boot order and "boot from CD next start", add / grow / detach
  disks (hot-plugged while running), add / remove NICs (virtio, e1000e, igb…; hot-plug, link up/down,
  move to another network).
- **SR-IOV labs**: igb NICs (Intel 82576, emulated SR-IOV: up to 7 VFs inside the guest) + a virtual IOMMU
  for vfio/DPDK in the guest, on any host; on hosts with real SR-IOV NICs, set VF counts and hand VFs to VMs
  through "SR-IOV VF pool" networks. See [docs/sriov.md](docs/sriov.md) (incl. OpenShift operator settings).
- **In-browser console** (noVNC) with Ctrl+Alt+Del, fullscreen, power buttons; the guest keyboard
  layout is set by cloud-init (defaults to your browser language, e.g. AZERTY for `fr`).
- **Live status**: libvirt events are pushed to the browser (Server-Sent Events), so a VM shutting
  down, a download progressing or a network stopping updates on screen without refreshing.
- **Cloud images**: one-click download of Ubuntu, Debian, AlmaLinux, Rocky and CentOS Stream images
  (or any URL). A VM gets its own full copy, grown to the requested size, plus a generated NoCloud
  seed ISO with its hostname, user, password and SSH keys.
- **Storage**: pools, volumes, ISO upload or download from URL.
- **Networks**: create NAT / routed / isolated networks, start/stop, autostart; edit subnet, DHCP range,
  domain and forward mode; static DHCP reservations (applied live, "make static" from a lease); raw XML editor.
- **Kubernetes clusters**: 1 or 3 control planes + N workers from a Debian / AlmaLinux cloud image;
  kubeconfig download (works from the host), `kubectl get nodes/pods` in the UI, start/stop, add/remove workers.
  - **k3s** on a standalone NAT network (no router) with fixed addresses and DNS (`api.<cluster>.<domain>`
    = first control plane); ready in ~2 minutes.
  - **kubeadm** (vanilla Kubernetes from pkgs.k8s.io, containerd, Flannel) inside a **lab group**: an
    existing one, or one created for the cluster and deleted with it. The group router serves the node
    leases and DNS (`api` / `api-int.<cluster>.<domain>`) and load-balances the API with haproxy over every
    control plane; the host reaches it on the router's reserved uplink address. Ready in ~3 min (Debian 13).
  The cluster page has copy-paste kubectl commands (bash/zsh/fish): one sets `KUBECONFIG` for the current
  shell, the other merges the cluster into `~/.kube/config` as context `<cluster>`. For example:
  ```bash
  mkdir -p $HOME/.kube && curl -fsS --create-file-mode 600 http://127.0.0.1:8000/api/v1/clusters/<id>/kubeconfig \
    -o $HOME/.kube/<name>.yaml && export KUBECONFIG=$HOME/.kube/<name>.yaml && kubectl get nodes
  ```
- **Lab groups**: an isolated network + a router VM (EL cloud image with dnsmasq + nftables: DHCP,
  DNS zone, NAT to an uplink) + member VMs with fixed MACs, static leases and `<member>.<domain>` names,
  plus custom DNS records (wildcards too) and TCP load balancers (haproxy on the router, reachable from the
  host on the router's reserved uplink address). Members and records are added/removed live (the router config
  is re-rendered and pushed through the QEMU guest agent); start/stop/delete the whole lab; export its spec.
  Other VMs attached to the group network get dynamic leases from the router: **"Make static"** turns one
  into a reservation (optionally with `<hostname>.<domain>`), and leases of stopped VMs can be released.
  Members come from a quick "Add member" row or the full Create VM form ("Custom VM…" on the Members tab,
  or "Lab group" in Create VM): cloud image, ISO install or empty disk; ISO/empty members have no cloud-init
  and get their reserved IP + name from the router by DHCP.
  **Remote access**: connect a laptop to a lab with WireGuard (router wg0, relayed by the host; config file /
  QR code, `nmcli connection import`), reaching the group network, its DNS zone and a cluster API: see
  [docs/wireguard.md](docs/wireguard.md).
- **OpenTofu provider** (`opentofu_provider/`) with examples in `examples/opentofu/`.
- **Tasks**: progress of background downloads, with cancel.

libvirt is the source of truth: VMs, pools and networks created with `virsh` or virt-manager show up
too. The SQLite database only keeps metadata (descriptions, cloud images, task history).

## Architecture

- **Backend**: FastAPI + SQLAlchemy + libvirt-python (`backend/`)
- **Frontend**: React + PatternFly 5 (`frontend/`), served by the backend once built
- **Hypervisor**: QEMU/KVM via `qemu:///system`

The backend runs directly on the host. Running it in a container is what broke libvirt access
before: it needs the libvirt socket, polkit authorization, and paths that QEMU (running as
`libvirt-qemu`) can read.

## Install

Supported hosts: **Arch** (and derivatives: CachyOS, Manjaro…), **Fedora**, **RHEL / AlmaLinux / Rocky /
CentOS Stream** 9 and 10, **Debian / Ubuntu**. Tested end to end on CachyOS, AlmaLinux 9 and 10 (SELinux
enforcing, firewalld) and Debian 13 (ufw).

```bash
# from a release tarball (web UI prebuilt, only Python needed on the host)
tar xzf vm-manager-<version>.tar.gz && cd vm-manager-<version>
scripts/setup.sh                 # then open http://127.0.0.1:8000

# from a git checkout: same, Node.js 20+ is used once to build the UI
```

`scripts/setup.sh` is idempotent (re-run it after an update). Run it as the user who will use VM Manager;
it uses sudo for the system parts:

1. installs libvirt, QEMU/KVM, OVMF, dnsmasq (+ `dhcp_release`), polkit/pkexec and the distro's libvirt Python bindings
2. starts libvirt (modular daemons on Fedora/RHEL, `libvirtd` elsewhere)
3. adds you to the `libvirt` group (the polkit rule that lets you manage VMs without a password)
4. installs the privileged helper `/usr/libexec/vm-manager/helper` (root-owned, fixed whitelist, run through
   `pkexec`, used to release DHCP leases) and `/etc/polkit-1/rules.d/50-vm-manager.rules`: the `libvirt`
   group may run that helper and start/stop the libvirt daemons and sockets (Start/Stop in the UI)
5. starts the `default` NAT network with autostart, and moves it to a free `192.168.X.0/24`
   when `192.168.122.0/24` is already in use (LAN, VPN, nested lab…)
6. firewall: with **ufw**, allows DHCP/DNS and forwarding on libvirt bridges (`virbr+`), otherwise VMs
   boot without an IP; with **firewalld**, libvirt's own `libvirt` zone already covers it. With either,
   opens udp 51820-51869 for lab remote access (WireGuard; `--wg-ports A-B|none`)
7. creates `backend/venv`, builds the UI if needed
8. installs the `vm-manager` systemd service (runs as you, listens on 127.0.0.1:8000)

| Option | |
|---|---|
| `--no-boot` | start libvirt and the service now but **don't enable them at boot** (gaming PC): `sudo systemctl start vm-manager` when needed |
| `--wg-ports A-B` | UDP ports opened in ufw/firewalld for lab WireGuard (default `51820-51869`, = `WG_HOST_PORTS`); `none` to skip |
| `--no-service` | no systemd service, start with `./run.sh` |
| `--listen 0.0.0.0 --port 8000` | reachable from the network (no login yet: trusted networks only; open the port yourself) |

Service: `journalctl -u vm-manager -f`, `sudo systemctl stop vm-manager`.

**libvirt on demand**: the app connects to libvirt only when a request needs it and closes the connection
after `LIBVIRT_IDLE_TIMEOUT` minutes once no browser tab is open, so socket-activated daemons can exit
(`--timeout 120`; a monolithic `libvirtd` stays up while it has active networks). The header shows
`libvirt: running/stopped`; **Start** / **Stop** (Host page) drive systemd without sudo. Stop refuses while
VMs run unless you pick "shut down all VMs first" or "stop anyway" (QEMU keeps running, unmanaged).
Remove the helper: `sudo rm -r /usr/libexec/vm-manager /usr/share/polkit-1/actions/org.vmmanager.helper.policy /etc/polkit-1/rules.d/50-vm-manager.rules`.

Release tarball: `scripts/package.sh` → `dist/vm-manager-<version>.tar.gz`.

## Run without the service

```bash
./run.sh                 # http://127.0.0.1:8000  (UI + API, docs at /docs)
./run.sh --dev           # backend with auto-reload on :8000
cd frontend && npm start # UI with hot reload on :3000, proxies /api to :8000
```

## Configuration

Environment variables or `backend/.env`:

| Variable | Default | |
|---|---|---|
| `LIBVIRT_URI` | `qemu:///system` | Start/Stop only for `qemu:///system` |
| `LIBVIRT_IDLE_TIMEOUT` | `5` | Minutes before an unused connection is closed when no browser is attached (`0` = never) |
| `LIBVIRT_DAEMON_MODE` | `auto` | `monolithic` (libvirtd) or `modular` (virtqemud…); auto-detected from systemd |
| `HELPER_PATH` | `/usr/libexec/vm-manager/helper` | Privileged helper (installed by setup.sh) |
| `DEFAULT_POOL_NAME` / `DEFAULT_POOL_PATH` | `default` / `/var/lib/libvirt/images` | Pool for new disks, ISOs and cloud images; created if missing |
| `DEFAULT_NETWORK` | `default` | Network for new VMs |
| `VNC_LISTEN` | `127.0.0.1` | `0.0.0.0` exposes VM consoles (no password) to the LAN |
| `DATABASE_URL` | `sqlite:///backend/data/vmanager.db` | |
| `CLUSTER_SUBNET_POOL` | `10.43.0.0/16` | New cluster networks get the first free /24 of it |

VNC servers listen on localhost; the web console reaches them through the backend's WebSocket bridge,
so nothing else needs to be exposed.

## OpenTofu

```bash
make -C opentofu_provider install
cd examples/opentofu/basic && tofu init && tofu apply     # one Debian VM "my-vm"
cd examples/opentofu/lab                                   # network + DHCP reservations + 2 VMs
cd examples/opentofu/k3s                                   # k3s cluster, kubeconfig as an output
cd examples/opentofu/kubeadm                               # kubeadm cluster in a lab group (auto-created or existing)
cd examples/opentofu/devices                               # extra disk, ISO in the CD-ROM, boot order
cd examples/opentofu/sriov                                 # virtio + igb NIC (vmmanager_nic), vIOMMU
cd examples/opentofu/group                                 # lab group: router + 2 members + DNS records
```

See `opentofu_provider/README.md` for all resources and arguments.

## Tests

`e2e/` drives the real UI in headless Chrome against real libvirt (creates and deletes `e2e-*` VMs):
`cd e2e && npm install && node smoke.js` (then `lifecycle.js`, `full.js`, `netedit.js`, `iso.js`, `kbd.js`,
`clusters.js`, `kubeadm.js`; the last ones need ~6 GB of RAM and use the host's `kubectl` if `KUBECTL` points at one).
`devices.js`, `nics.js`, `groups.js`, `group-members.js`, `group-dhcp.js`).
`libvirtctl.js` **stops libvirt**: run it only against a nested test install (see its header).

## API overview

| | |
|---|---|
| `GET/POST /api/v1/vms`, `GET/PATCH/DELETE /api/v1/vms/{id}` | VMs (`?delete_disks=true` on delete) |
| `POST /api/v1/vms/{id}/{start,stop,force_stop,reboot,suspend,resume}` | Power actions |
| `PUT /api/v1/vms/{id}/cdrom` `{iso_path\|null}` | Insert / eject an ISO (live) |
| `PUT /api/v1/vms/{id}/boot` `{order?, once?}` | Boot order; `once: true` = boot the CD on the next start only |
| `POST /api/v1/vms/{id}/disks`, `PUT/DELETE …/disks/{target}` | Add (hot-plug), grow, detach disks (`?delete_volume=true`) |
| `POST /api/v1/vms/{id}/nics` `{network, model?, mac?, link_state?}`, `PUT/DELETE …/nics/{mac}` | Add (hot-plug), change link state / network (live), remove NICs |
| `PUT /api/v1/vms/{id}/iommu` `{enabled}` | Virtual IOMMU (applies at the next cold start) |
| `GET /api/v1/hosts/sriov`, `PUT /api/v1/hosts/sriov/{pf}` `{num_vfs}` | Host IOMMU state, SR-IOV PFs / VFs; set the VF count (helper) |
| `GET /api/v1/storage/cloud-images`, `POST` (download), `GET …/distributions` | Cloud images |
| `GET /api/v1/storage/isos`, `POST …/isos/upload`, `POST …/isos/download` | ISOs |
| `/api/v1/storage/pools`, `/api/v1/storage/volumes` | Pools and volumes |
| `/api/v1/networks`, `…/{id}/start`, `…/{id}/stop`, `…/{id}/autostart`, `…/{id}/leases` | Networks |
| `GET /api/v1/tasks`, `POST /api/v1/tasks/{id}/cancel` | Background tasks |
| `GET /api/v1/events` | Live events (Server-Sent Events) |
| `WS /api/v1/vms/{id}/vnc` | VNC console (WebSocket) |
| `GET /api/v1/networks/{id}/config`, `PUT /api/v1/networks/{id}`, `PUT …/xml`, `POST/PUT/DELETE …/hosts` | Network editing, DHCP reservations |
| `GET /api/v1/hosts/info`, `GET /api/v1/hosts/resources` | Host info and setup issues |
| `GET /api/v1/hosts/libvirt`, `POST …/libvirt/start`, `POST …/libvirt/stop {mode: refuse\|shutdown\|force}` | libvirt daemon state / start / stop; other endpoints answer `503 {"detail": "libvirt is stopped"}` while it is down |
| `DELETE /api/v1/networks/{id}/leases/{mac}`, `DELETE …/hosts/{mac}?release_lease=true` | Release a DHCP lease (dnsmasq `dhcp_release` via the helper) |
| `GET/POST /api/v1/clusters`, `GET/DELETE …/{id}`, `POST …/{id}/{start,stop}` | Kubernetes clusters (create/start/stop are tasks) |
| `GET …/clusters/{id}/kubeconfig`, `GET …/{id}/kubectl/{nodes,pods}`, `POST …/{id}/workers`, `DELETE …/{id}/nodes/{name}` | Kubeconfig, kubectl views, scaling |
| `GET/POST /api/v1/groups`, `GET/PUT/DELETE /api/v1/groups/{id}` (spec; `?delete_disks=`) | Lab groups (create runs as a task) |
| `POST …/groups/{id}/{start,stop}`, `POST/DELETE …/members`, `POST/DELETE …/dns-records` | Group power (router first on start, last on stop), live members / records |
| `GET …/groups/{id}/router/config`, `POST …/router/apply`, `GET …/export` | Rendered router config, re-push, spec YAML |
| `GET/POST …/groups/{id}/dhcp-hosts`, `PUT/DELETE …/dhcp-hosts/{mac}` (`?release_lease=`) | Static reservations of non-member machines (live on the router) |
| `GET …/groups/{id}/leases`, `DELETE …/leases/{mac}` (`?force=`) | Router leases (member / reservation / dynamic), release one |

Full interactive docs: `/docs`.

## Roadmap

See [future-features.md](future-features.md): lab groups v2 (BGP, WireGuard, VLANs, snapshots, templates, VyOS router),
then **Kubernetes / OpenShift** clusters on top of groups.

## Containers

`docker-compose.yml` is kept for reference but is not the supported way to run the backend (see
Architecture). Use `scripts/setup.sh`.
