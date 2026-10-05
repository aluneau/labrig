# Future features: design notes and roadmap

> Goal: an easy way to build labs that **reproduce customer
> cases**, running on an Arch Linux gaming rig without rebooting into anything else, and the **same
> software on RHEL lab machines**. Next big step: **lab groups**, a set of VMs on their own network
> behind a networking VM (DNS, DHCP, BGP, WireGuard…). After that, **Kubernetes / OpenShift clusters**
> built on groups.

Each section gives the design, the API / UI / OpenTofu impact and a rough effort (S ≤ 1 day,
M ≈ 2–4 days, L ≈ 1–2 weeks).

---

## 1. Smaller features

### 1.1 Remove / release a DHCP lease (S) — ✅ done

Implemented as recommended: `DELETE /networks/{id}/leases/{mac}` (409 if a running VM owns the MAC,
unless `force`) and `DELETE …/hosts/{mac}?release_lease=true`; the helper's `dhcp-release <net> <ip> <mac>`
resolves the bridge and checks the lease itself (passes the client id too). setup.sh installs
`dnsmasq-utils` (Debian, EL) / `dnsmasq` (Arch). Verified on AlmaLinux 9 (SELinux) and Debian 13.

libvirt has no API to delete a lease: `virNetworkGetDHCPLeases` is read-only. libvirt runs one
dnsmasq per network, and dnsmasq calls libvirt's `leaseshelper` on every lease change. The helper
keeps `/var/lib/libvirt/dnsmasq/<bridge>.status` up to date, and that file is what
`DHCPLeases()` reads.

| Option | How | Pros | Cons |
|---|---|---|---|
| **`dhcp_release`** (recommended) | `dhcp_release <bridge> <ip> <mac>` sends a DHCPRELEASE to dnsmasq, which drops the lease and calls the helper | clean, live, other VMs unaffected | needs root and the `dnsmasq-utils` / `dnsmasq` tools package; IPv4 only ([man](https://man.archlinux.org/man/dhcp_release.1.en)) |
| Edit `<bridge>.status` + SIGHUP | rewrite the JSON, reload dnsmasq | no extra package | racy with dnsmasq, root, relies on libvirt internals |
| Restart the network | `destroy` + `create` | uses the API we already have | all VMs on it lose connectivity briefly; every lease goes |

A running guest re-requests its address at renewal, so releasing a lease is mostly useful for
**stopped / deleted VMs** and **after removing a static reservation**. UI: a "Release" action on
each lease row of *Network → DHCP*, offered when the MAC has no running VM, plus a "release the
current lease too" checkbox when removing a reservation.

The app runs as an unprivileged user, so this needs the privileged helper from §1.5 (a polkit-gated
command such as `vmm-helper dhcp-release <network> <mac>`). Never give the app sudo.

API: `DELETE /api/v1/networks/{id}/leases/{mac}`. OpenTofu: none.

### 1.2 Attach / eject an ISO from the VMs tab (S) — ✅ done

- `PUT /api/v1/vms/{id}/cdrom {iso_path | null}`. If the VM has no CD-ROM device: add a SATA one
  with `attachDeviceFlags(..., CONFIG)`. SATA CD-ROMs can't be hot-added, so it takes effect at next
  start. Otherwise swap the media with `updateDeviceFlags(<disk device='cdrom'>…</disk>, LIVE|CONFIG)`.
  This works live, like ejecting and inserting a disc.
- To make this frictionless, give **every new VM an empty SATA CD-ROM** (one line in the domain
  template). Then changing the media is always live.
- UI: VM details → "CD/DVD" row: ISO dropdown (from `GET /storage/isos`) plus an Eject button. The same
  control goes in the console page toolbar, which is where you need it during an install.

### 1.3 Boot order and "boot from ISO on next boot" (S–M) — ✅ done (recommended option; up/down list in the UI)

The domain XML currently uses `<os><boot dev='hd'/><boot dev='cdrom'/></os>`. Two options:

1. **Persistent order**: rewrite `<os><boot>` (or switch to per-device `<boot order='N'/>`; the two
   can't be mixed) through `defineXML`. Applies at the next cold start. UI: a draggable list of boot
   devices.
2. **One-shot "boot from ISO next time"** (the main use case). libvirt has no one-shot flag. Two
   ways to get the same effect:
   - **Recommended**: the app stores `next_boot = cdrom` in the DB. On the next start *through the app*
     it defines the XML with CD first, starts the VM, then immediately redefines the persistent XML
     with the original order. The running instance keeps CD first, and the following boot is back to
     disk. Restarts from inside the guest keep the running config (same as a real PC).
   - Alternative: rely on SeaBIOS/OVMF `menu='yes'` with a boot menu delay and press F12 in the console.
     Zero code, but manual.

API: `PUT /vms/{id}/boot {order: ["cdrom","hd"], once?: true}`. OpenTofu: `boot_order` on `vmmanager_vm`
(in-place).

### 1.4 Add / remove disks on an existing VM (S–M) — ✅ done (+ `vmmanager_disk`, `boot_order`/`cdrom` on `vmmanager_vm`)

Note: libvirt does **not** add spare pcie-root-ports by itself; new VMs now declare 16. VMs created
before that have ~1 free port, further hot-adds are attached for the next start.

- `POST /vms/{id}/disks {size_gb, pool?, format=qcow2, bus=virtio}` creates a volume
  `<vm>-disk<N>.qcow2`, then `attachDeviceFlags(<disk … target dev='vdX'/>, LIVE|CONFIG)`.
  virtio-blk hot-plugs on q35 (pcie-root-port slots are available because libvirt adds spare ports).
  Pick the next free `vdX` from the XML.
- `DELETE /vms/{id}/disks/{target}?delete_volume=true` runs `detachDeviceFlags`. Hot-unplug needs the
  guest's cooperation: poll for the device-removed event, and fall back to CONFIG-only with "applies at
  next shutdown". Refuse to detach the boot disk.
- Resize: `vol.resize()` plus `domain.blockResize()` when running.
- UI: Disks table in VM details with Add / Resize / Detach. OpenTofu: a separate `vmmanager_disk`
  resource (`vm_id`, `size`) so disks can be added without recreating the VM.

### 1.5 Start / stop libvirt from the app (gaming rig) (M) — ✅ done

Implemented with the "simpler alternative" for start/stop (polkit rule on
`org.freedesktop.systemd1.manage-units` for libvirt units only, `systemctl` as the app user) and the
pkexec helper for DHCP release. Connect on demand + idle close (`LIBVIRT_IDLE_TIMEOUT`), state from
`systemctl show` (`GET /hosts/libvirt`), 503 `libvirt is stopped`, header pill, gated pages, Host card with
stop modes `refuse | shutdown | force`. Verified in nested AlmaLinux 9 (modular, SELinux enforcing:
virtqemud exits ~2 min after the app's idle close and is socket-activated again) and Debian 13
(monolithic). Note: a monolithic `libvirtd` doesn't honour `--timeout` while networks are active, so on
Arch/Debian it stays up until stopped. Original design notes:

Requirements: libvirt is **not** enabled at boot (`setup.sh --no-boot`), and the app must not keep it
alive.

Current problem: `main.py`'s `keep_connected()` watchdog reconnects every 5 s, and the open
connection (plus the SSE keepalive) prevents socket-activated `libvirtd --timeout 120` (or
`virtqemud`) from ever exiting.

Design:
- **Backend states**: `libvirt: running | stopped | starting`. Replace the watchdog with: connect on
  demand. When a request fails because the daemon is down, return `503 {"detail": "libvirt is stopped"}`
  instead of a generic 400. Publish `{"kind":"connection","event":"disconnected"}` (already sent by the
  close callback). With no browser tab open, nothing keeps the connection: close it after N minutes
  idle, so the daemon can time out.
- **Privileged helper**: a tiny root-owned script, `/usr/libexec/vm-manager/helper`, with a fixed
  command whitelist: `libvirt-start`, `libvirt-stop`, `dhcp-release …`. It is run through
  `pkexec`, and a polkit rule installed by `setup.sh` lets the `libvirt` group run that action
  without a password (`org.vmmanager.helper`). Simpler alternative: a polkit rule allowing the
  group `libvirt` to `start`/`stop` the units `libvirtd.service`, `virtqemud.*`,
  `virtnetworkd.*` through `org.freedesktop.systemd1.manage-units` (check `action.lookup("unit")`), and
  call systemd over D-Bus. No helper binary for that part.
- **Stop semantics**: refuse (or offer "shut down all VMs first") when VMs are running. Stopping
  libvirtd doesn't kill QEMU processes, so warn about orphaned guests.
- **UI**: header pill `libvirt: stopped [Start]`. Every page shows a "libvirt is stopped" empty state
  with a Start button instead of errors. Host page: Start/Stop.
- The vm-manager service itself can stay installed but not enabled (`--no-boot`). It's light, and
  only the daemon + VMs matter for gaming performance.

---

## 2. Lab groups

> **Status: v1 done** (2026-10). Isolated network + EL router (dnsmasq + nftables, AlmaLinux 9/10) +
> members with fixed MACs/static leases/DNS names + DNS records (A, CNAME, wildcard), live updates through
> the guest agent, start/stop ordering, delete (keep or delete disks), DB rebuild from libvirt metadata,
> Groups UI (topology, members, network & DNS, router config, export), `vmmanager_group`, `e2e/groups.js`.
> Not yet: FRR/BGP, WireGuard, VLANs (accepted in the spec, rejected with "not supported yet"), VyOS
> flavour, groups without uplink (the EL router installs its packages at first boot), snapshots, templates,
> export with disks, per-group autostart, `group_id` on `vmmanager_vm`.
>
> **Custom members** (done): the full Create VM form adds members too ("Custom VM…" on the Members tab,
> "Lab group" select on the VMs page). `MemberSpec.source` = `cloud_image` | `iso` | `empty` (+ `iso`), so
> ISO installs live in the spec like the rest (router metadata, rebuild, export). ISO/empty members get no
> cloud-init: only the MAC reservation + DNS name, the installed OS must use DHCP. Next: the OpenTofu
> `vmmanager_group` `member` block should gain `source`, `iso`, `cloud_init` and `user_data` (it only has
> name/image/memory/vcpu/disk_size/role/ip today); PXE boot from the router (dnsmasq `dhcp-boot`) would
> make empty-disk members useful (they boot nothing until an ISO is inserted on the VM's Devices tab).

### 2.1 Concept and data model (L)

A **group** = an isolated libvirt network + a **router VM** + member VMs + a DNS zone. It is the unit
you start, stop, snapshot, export and delete.

```text
group "case-12345"                       host bridge / default NAT (uplink)
 ├─ network  vmm-g-case-12345  10.42.7.0/24 (isolated, no libvirt DHCP/DNS)
 ├─ router   case-12345-rtr    eth0 → uplink (NAT/route), eth1 → group net (.1)
 │           DHCP + DNS (zone case-12345.lab) + NTP + optional BGP / WireGuard / VLANs / proxy
 └─ members  web1, db1, ocp-ctl-0…  (static leases from the group definition)
```

DB tables: `groups(id, name, cidr, domain, router_vm_id, uplink, spec JSON, status)` and
`group_members(group_id, vm_id, role, ip, mac, hostname)`. The **spec is the source of truth**
(declarative), and the router config is *generated* from it. libvirt objects carry
`<metadata><vmm:group name='…' role='…'/></metadata>` so the DB can be rebuilt from libvirt (keeping
the rule "libvirt is the source of truth").

Why a router VM instead of libvirt's dnsmasq: customer cases need things libvirt networks don't do,
such as BGP peering, VLAN trunks, WireGuard tunnels, odd DNS (split horizon, forwarders, broken
records on purpose), MTU problems, proxies, firewall rules. The router also makes a group portable:
it behaves the same on another host.

### 2.2 Choosing the router OS

Criteria: configured **from a file / cloud-init** (no clicking), DNS+DHCP, BGP, WireGuard, VLANs,
image availability and licence, footprint, relevance to Red Hat customer setups.

| | Config from file | DNS/DHCP | BGP | WireGuard | VLAN | Image / licence | Footprint | Notes |
|---|---|---|---|---|---|---|---|---|
| **Fedora/CentOS Stream/RHEL cloud image + dnsmasq (or unbound+kea) + nftables + FRR + WireGuard, via cloud-init** | ✅ cloud-init `write_files` + `runcmd`; we already build NoCloud seeds | ✅ | ✅ FRR (same as RHEL) | ✅ in-kernel | ✅ NetworkManager | ✅ free images (CentOS Stream/Alma/Fedora; RHEL with subscription) | ~512 MB RAM, 2 GB disk | **Same stack customers run on RHEL**; everything is plain text config we template |
| **VyOS** | ✅ cloud-init `vyos_config_commands` (set-style commands) ([docs](https://docs.vyos.io/en/1.5/automation/cloud-init.html)) | ✅ | ✅ FRR | ✅ | ✅ | ⚠️ free *rolling/Stream* images ([downloads](https://downloads.vyos.io/), [Stream 2025.11](https://blog.vyos.io/vyos-stream-2025.11)); LTS qcow2 needs a subscription; cloud-init qcow2 often self-built ([vyos-vm-images](https://github.com/vyos-contrib/packer-vyos)) | ~512 MB | Best "router appliance" UX and CLI; reproduces network-team setups well |
| **OpenWrt x86-64** | ✅ UCI files dropped in `/etc/uci-defaults` (run once at first boot) ([docs](https://openwrt.org/docs/guide-developer/uci-defaults)) | ✅ dnsmasq | ⚠️ FRR/bird packages, less common | ✅ | ✅ | ✅ GPL, tiny `combined-ext4` images ([x86](https://openwrt.org/docs/guide-user/installation/openwrt_x86)) | ~64 MB | No cloud-init: inject the config by editing the image (guestfish) or a second disk; far from enterprise setups |
| **OPNsense / pfSense** | ⚠️ one big `config.xml`, imported from a `conf/` dir on an ISO at install ([example](https://git.nationtech.io/NationTech/harmony/src/branch/master/docs/use-cases/opnsense-vm-integration.md), [confgen](https://pypi.org/project/opnsense-confgen)) | ✅ | ✅ plugin | ✅ | ✅ | ✅ OPNsense BSD; pfSense CE weaker | ~1 GB | Great GUI for humans; XML schema per version is brittle to generate; FreeBSD |

**Recommendation:** default router = **a minimal CentOS Stream / AlmaLinux (or RHEL) cloud image
configured by cloud-init**, with dnsmasq (DHCP + DNS + static hosts), nftables (NAT/firewall), FRR
(BGP/OSPF when enabled), WireGuard and NetworkManager VLANs. Why:
- It reuses what the app already does (cloud images + NoCloud seed).
- Every service is a plain config file, easy to template from the group spec and easy to tweak by
  hand for a customer case.
- It is exactly the RHEL stack customers run, so a reproduction behaves the same way.
- For OpenShift labs it can also host haproxy (API/ingress LB), an HTTP server and a mirror registry.

**VyOS as a first-class flavour** (the owner likes it, and it fits well): one config tree
(`set interfaces …`, `set protocols bgp …`, `set service dhcp-server …`) that we generate from the
same spec and pass as cloud-init `vyos_config_commands`. It gives a real router CLI to debug a
customer's BGP/VPN design, and `show configuration commands` doubles as a readable export of the lab.
Caveat to handle in the app: free images are **rolling/Stream** only. The LTS qcow2 needs a
subscription, and Stream ISOs need a one-time conversion to a cloud-init qcow2 (or a build with
[vyos-vm-images](https://github.com/vyos-contrib/packer-vyos)). So the cloud-image list would get a
"VyOS (bring your own image / Stream URL)" entry rather than a fixed URL. Prototype both flavours on
the same spec in Groups v1 and keep whichever feels better as the default. The generator sits behind
an interface (`RouterBackend.render(spec) -> cloud-init`), so OPNsense/OpenWrt can be added later if a
case requires it.

### 2.3 Generating the router config

The group spec (YAML/JSON, also what OpenTofu sends):

```yaml
name: case-12345
cidr: 10.42.7.0/24
domain: case-12345.lab
uplink: default            # libvirt network for internet (NAT) or a host bridge
router:
  flavour: el              # el | vyos
  dns:
    forwarders: [1.1.1.1]
    records: [{name: api.ocp, a: 10.42.7.10}, {name: "*.apps.ocp", a: 10.42.7.11}]
  bgp: {asn: 64512, neighbors: [{ip: 10.42.7.20, asn: 64513}]}   # optional
  wireguard: {listen_port: 51820, peers: []}                       # optional
  vlans: [{id: 100, cidr: 10.42.100.0/24}]                         # optional
members:
  - {name: web1, image: almalinux-9, memory: 2048, ip: 10.42.7.21}
```

`router_service.render(spec)` → cloud-init user-data with `write_files`:
`/etc/dnsmasq.d/group.conf` (dhcp-range, `dhcp-host=` per member, `address=`/`host-record=` per DNS
record, `domain=`), `/etc/nftables/group.nft`, `/etc/frr/frr.conf`, `/etc/wireguard/wg0.conf`,
NM keyfiles for VLANs, then `runcmd` to enable the services.

**Updates after creation**: re-render, push over SSH (key generated per group, stored in the DB) or the
QEMU guest agent (`guest-file-write` + `guest-exec`, no network needed), and reload the services. Same
idea as the live DHCP reservations, but with the router as the target.

### 2.4 API / UI / OpenTofu

- API: `GET/POST /groups`, `GET/PUT/DELETE /groups/{id}` (spec), `POST /groups/{id}/{start|stop}`
  (router first on start, last on stop), `POST /groups/{id}/members`, `GET /groups/{id}/router/config`
  (rendered files, for debugging), events `{"kind":"group", …}`.
- UI: a **Groups** page (cards with status and member count). Group detail with tabs: *Topology*
  (simple SVG: uplink, router, members), *Members*, *Network & DNS* (records, reservations), *Router*
  (rendered config + "Open router console"), *Export* (spec YAML / OpenTofu).
- OpenTofu: `vmmanager_group` (network, router, DNS) plus `group_id` on `vmmanager_vm`. Alternatively a
  `vmmanager_group_member`. Keeping the group a single resource with nested `dns_records` / `bgp` /
  `wireguard` blocks matches how people think about a lab.

### 2.5 Lifecycle and extras

- **Start/stop the whole group** with ordering (router → infra → others) and a per-group "autostart".
- **Snapshots**: external qcow2 snapshots of all members (`snapshotCreateXML` with disk-only + memory
  optional) → "save the lab before trying the customer's fix" and revert. Effort M.
- **Clone / export**: export spec + disk overlays to a tarball, so a reproducer can be shared with a
  colleague on another host (same software on RHEL lab machines).
- **Inter-group connectivity**: (a) groups share an "interconnect" libvirt network and routers peer
  with **BGP**, or (b) a **WireGuard** tunnel between routers, possibly across hosts (rig ↔ RH lab).
  Both fit in the spec as `peers: [{group: other, via: bgp|wireguard}]`.
- **Customer-case templates**: a library of specs (`templates/*.yaml`) such as "split DNS",
  "BGP + MetalLB", "proxy-only egress", "MTU 1400 path", "disconnected (no uplink) + mirror registry".
  You'd pick a template, fill in a case number, and press Create.

---

## 3. Kubernetes / OpenShift on top of groups

The router VM supplies what clusters expect from "the datacenter": DNS records, DHCP reservations, NTP,
load balancer (haproxy) or VIPs, HTTP for artefacts, and optionally a mirror registry for disconnected
cases.

### 3.1 What kcli does (and what to learn from it)

[kcli](https://github.com/karmab/kcli) (Karim Boumedhel, Red Hat; **Apache-2.0**; pip / Copr RPM) is
the reference tool for this on libvirt ([docs](https://kcli.readthedocs.io/en/latest/)):
- **Profiles** (reusable VM templates) and **plans** (Jinja2-templated YAML with VMs, networks,
  disks, DNS entries and clusters, plus `-P key=value` parameters).
- `kcli create cluster openshift|okd|kubeadm|k3s|rke2|microshift|hypershift -P ctlplanes=3 -P workers=2`;
  assets and kubeconfig land in `~/.kcli/clusters/<name>/`.
- OpenShift on libvirt: drives the official `openshift-install`. API/ingress **VIPs** reserved outside
  the DHCP range, made highly available inside the cluster with **keepalived** and spread across
  routers with **haproxy** static pods. **CoreDNS/mDNS** static pods remove the need for external DNS.
  SNO and the agent-based installer are supported. VIPs are guessed on virtual networks when not
  given.

Integration options:
1. **Use kcli as an engine** (`kcli create cluster … -P …` in a task, network = the group's network).
   Fastest path to "OpenShift in the UI". Downsides: a big extra dependency, its own state in
   `~/.kcli`, and VMs we don't create ourselves (they would still show up, since libvirt is the source
   of truth). Good as an **"Advanced: deploy with kcli"** option.
2. **Native** (recommended long term): reuse our own VM/cloud-init/group primitives and the official
   installers directly. It's smaller and easier to reason about, and the router VM handles DNS/LB
   cleanly instead of in-cluster keepalived/mDNS tricks, which is closer to how customers deploy
   (external DNS + LB). Borrow kcli's parameter names and defaults so users of both feel at home.

### 3.2 Phase A: Kubernetes (M)

- **k3s: done** (`cluster_service.py`, `cluster_drivers.py`, `cluster_network.py`, *Clusters* page,
  `vmmanager_cluster`, `e2e/clusters.js`). Nodes = cloud-image VMs created with `vm_service`; cloud-init
  writes `/etc/rancher/k3s/config.yaml` (app-generated token, `tls-san`, `node-ip`, pod/service CIDRs
  10.244/16 + 10.96/16 so they never collide with node networks) and runs the get.k3s.io installer
  (`INSTALL_K3S_VERSION` when pinned). 1 control plane, or 3/5 with embedded etcd (`cluster-init`, the
  others join `https://api.<cluster>.<domain>:6443`). Creation is a task: network → nodes → wait for the
  guest agent → wait until every node is Ready (`kubectl get nodes -o json` through guest-exec) → fetch
  the kubeconfig (server rewritten to `https://<ctlplane-0 IP>:6443`, reachable from the host). Verified:
  Debian 13 and AlmaLinux 9 (SELinux enforcing; the installer adds `k3s-selinux`; the app makes only
  `virt_qemu_ga_t` permissive and re-enables guest-exec in `/etc/sysconfig/qemu-ga`), 1+2 in ~1–2 min,
  3 control planes, add/remove workers, stop/start (start waits for Ready reported after the boot),
  rebuild of the `clusters` table from `<vmm:cluster>` node metadata (token and kubeconfig re-read from
  the first control plane). Not done: an API load balancer for 3 control planes (the `api` record points at
  ctlplane-0 only), kubeadm.
- **Network seam for groups.** Clusters only talk to `ClusterNetwork` (`ensure`, `allocate_ip`,
  `reserve(host, mac, ip)`, `release(mac)`, `publish(ip, names)`, `unpublish(ip)`, `subnet`, `destroy`),
  chosen in one place: `cluster_service.network_for(cluster)`. v1 is `LibvirtClusterNetwork`: an owned NAT
  network `vmm-k-<cluster>` (or an existing one) whose dnsmasq serves the reservations (`net.update`
  DHCP host) and `<dns><host>` records. To put clusters in a lab group: add `GroupClusterNetwork(group)`
  where `ensure` = the group exists and runs, `allocate_ip`/`reserve` = add a member with a fixed MAC/IP
  to the group spec, `publish` = add A records (`api`, `api-int`, nodes, later `*.apps`) to the group's
  DNS records, and re-render + push the router config (`router_service`); `destroy` = nothing (the group
  owns the network). Then `ClusterCreate` gets `group_id`, `network_for` returns the group variant when
  `cluster.group_id` is set, and the router's haproxy can front `api` for 3 control planes (kubeadm/OCP).
- **kubeadm** (closer to vanilla / customer setups): cloud-init installs containerd + kubeadm from
  pkgs.k8s.io, `kubeadm init --control-plane-endpoint api.<domain>:6443` with a pre-generated token +
  certificate key; other nodes `kubeadm join`. The router's haproxy fronts the API for 3 control planes.
- The app fetches the kubeconfig (via the guest agent or SSH) and exposes it as a download.
- Cluster = a **group with a role layout** (`ctlplanes`, `workers`, `type`) + a `clusters` table
  (`kubeconfig`, `version`, `status`). UI: *Clusters* page, kubeconfig download, console of each node.

### 3.3 Phase B: OpenShift SNO with the agent-based installer (L)

Requirements: SNO minimum **8 vCPU, 16 GB RAM, 120 GB disk** (4 vCPU works, but with no headroom),
and DNS for `api.<cluster>.<domain>`, `api-int.<cluster>.<domain>`, `*.apps.<cluster>.<domain>`.
For SNO all three point to the node's IP ([SNO docs](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/installing_on_a_single_node/install-sno-installing-sno),
[agent-based](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/installing_an_on-premise_cluster_with_the_agent-based_installer/installing-with-agent-based-installer)).
A 3-node compact cluster needs ~3×(8 vCPU, 16–24 GB). It fits a gaming rig with 64+ GB, which is
the point of this project.

Flow:
1. User supplies a **pull secret** once (stored encrypted at rest in `backend/data`, never sent to the
   browser again) and picks a version. The app downloads `openshift-install` for that version
   (mirror.openshift.com) into a cache.
2. The app writes `install-config.yaml` (baseDomain = group domain, machineNetwork = group CIDR,
   platform `none` for SNO, `baremetal` with VIPs for multi-node) and `agent-config.yaml`
   (rendezvousIP, a static MAC→IP per host, matching the group's DHCP reservations), then runs
   `openshift-install agent create image` (a task) and uploads `agent.x86_64.iso` into the pool.
3. Creates the node VMs (fixed MACs, empty 120 GB disks, CD-ROM = agent ISO, boot order disk → cdrom),
   and the router gets the `api`/`api-int`/`*.apps` records (SNO) or haproxy + VIPs (compact/HA).
4. Runs `openshift-install agent wait-for install-complete` in a task with progress and log
   streaming; on success it stores the kubeconfig + kubeadmin password and ejects the ISO (§1.2).

Phase C: **compact (3 masters) and HA** with workers, **disconnected** mode (mirror registry on the router or
a helper VM, `oc-mirror`), and **OKD** (no pull secret) for people without subscriptions.

OpenTofu: `vmmanager_cluster { type = "openshift" | "k3s" | "kubeadm", group_id, ctlplanes, workers,
version, pull_secret = file(...) }` with computed `kubeconfig` (sensitive) and `console_url`.

---

## 4. Suggested order

1. **§1.5 Start/stop libvirt + no idle keepalive** (it's a gaming rig: this matters every day), then
   **§1.2 ISO attach/eject + §1.3 boot once + §1.4 disks** (these unblock ISO-based installs, including
   the OpenShift agent ISO later), then **§1.1 DHCP release** (reuses the §1.5 helper).
2. **Groups v1**: isolated network + EL router (dnsmasq + nftables) + members + DNS records; UI page;
   `vmmanager_group`. Then v2: FRR/BGP, WireGuard, VLANs, snapshots, templates, export.
3. **Clusters**: k3s → kubeadm (HA behind router haproxy) → OpenShift SNO (agent-based) →
   compact/HA → disconnected. Offer "deploy with kcli" early as an escape hatch if a case needs a
   topology we don't support natively yet.
4. Cross-cutting, before sharing widely: **authentication** (at least a local user + token; the API
   can create VMs on the host), and a per-group **resource budget** check (CPU/RAM/disk) before
   creating, so a lab doesn't starve the host.

### Sources
- kcli: https://github.com/karmab/kcli, https://kcli.readthedocs.io/en/latest/
- VyOS cloud-init: https://docs.vyos.io/en/1.5/automation/cloud-init.html; images: https://downloads.vyos.io/, https://blog.vyos.io/vyos-stream-2025.11
- OpenWrt: https://openwrt.org/docs/guide-developer/uci-defaults, https://openwrt.org/docs/guide-user/installation/openwrt_x86
- OPNsense automation: https://git.nationtech.io/NationTech/harmony/src/branch/master/docs/use-cases/opnsense-vm-integration.md, https://pypi.org/project/opnsense-confgen
- dnsmasq `dhcp_release`: https://man.archlinux.org/man/dhcp_release.1.en
- OpenShift SNO / agent-based installer: https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/installing_on_a_single_node/install-sno-installing-sno, https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/installing_an_on-premise_cluster_with_the_agent-based_installer/installing-with-agent-based-installer
