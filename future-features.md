# Future features: design notes and roadmap

> Goal: an easy way to build labs that **reproduce customer cases**, running on a desktop Linux host
> without rebooting into anything else, and the **same software on RHEL lab machines**. Next big step: **lab groups**, a set of VMs on their own network
> behind a networking VM (DNS, DHCP, BGP, WireGuard…). After that, **Kubernetes / OpenShift clusters**
> built on groups.

Each section gives the design, the API / UI / OpenTofu impact and a rough effort (S ≤ 1 day,
M ≈ 2–4 days, L ≈ 1–2 weeks).

## Status at a glance (2026-10-05)

| Area | State |
|---|---|
| §1.1–1.6 small features (lease release, ISO/boot/disks, libvirt on demand, NICs + SR-IOV) | ✅ done, OpenTofu included |
| Console fidelity (VNC looked dark) | ✅ done: sharp scaling, 1:1/Fit toggle (§1.7) |
| §2 Lab groups v1 + static reservations + custom (ISO/empty) members | ✅ done; v2 items open (§2.5) |
| §3.2 k3s (standalone network) and kubeadm (in a lab group, router haproxy) | ✅ done |
| Lab remote access: WireGuard on the group router, laptop joins a lab (docs/wireguard.md) | ✅ done |
| BGP on the group router (FRR) + MetalLB BGP mode + self-explaining Topology view (docs/bgp.md) | ✅ done |
| §3.3 OpenShift (agent-based installer: SNO verified; compact/HA, ODF untested) + add-ons, MetalLB L2/BGP, topology view | ✅ done (2026-10-05) |
| Disconnected labs: router egress switch + mirror registry on the router (mirror-registry, oc-mirror v2, own images; docs/disconnected.md) | ✅ done (2026-10-06); OpenShift disconnected install next |
| Customer-case templates (§2.5): gallery, params → review (editable YAML, OpenTofu, guide) → create, Case guide tab, save group as template (docs/templates.md) | ✅ done (2026-10-09) |
| Authentication, CI, per-group resource budget | open (§4) |

OpenTofu covers every feature above: `vmmanager_cloud_image`, `_network` (incl. `mode = "hostdev"` VF pools),
`_vm` (`boot_order`, `cdrom`, `iommu`, `guest_kernel_args`), `_disk`, `_nic`, `_group` (members with
`source`/`iso`/`cloud_init`/`user_data`, `dns_record`, `dhcp_host`, `router_memory`, `wireguard`, `bgp`),
`_wireguard_peer`, `_cluster` (k3s, kubeadm, `group_id`). Not exposed (by design): libvirt start/stop, lease release (imperative actions).

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

### 1.6 Networking: multiple NICs and SR-IOV (M) — ✅ done

- **Done**: NICs add (hot-plug) / remove (hot-unplug) / link up-down / move to another network, models virtio,
  e1000e, igb, e1000, rtl8139; extra NICs at creation; DHCP on every NIC for Debian/Ubuntu cloud images;
  virtual IOMMU (intel, intremap, caching mode) + guest kernel args via cloud-init; igb = emulated SR-IOV
  (7 VFs, igbvf, vfio-pci bind + DPDK testpmd verified in Debian 13 / AlmaLinux 10); host SR-IOV view + VF
  count (helper `sriov-set-numvfs`); "SR-IOV VF pool" networks (`forward mode='hostdev'`), VF hot-plug, verified
  nested (L2 on a VF of L1's igb). `vmmanager_nic`, `iommu` / `guest_kernel_args` on `vmmanager_vm`,
  `vmmanager_network` `mode = "hostdev"` (verified nested: VF pool on L1's igb + VM with a VF NIC, clean plan).
  docs/sriov.md has the OpenShift operator settings (82576 is not in OpenShift's supported list).
- **Not yet**: macvtap / bridge-type NICs (only libvirt networks), VLAN trunks on NICs, NIC options in group /
  cluster specs (e.g. workers with an igb NIC for SR-IOV operator labs), persisting VF counts from the UI,
  `<driver queues>` (multiqueue) and NIC MTU.

### 1.7 Console fidelity (S) — ✅ done

The owner found the VNC console "dark". Measured (guest framebuffer from `virsh screenshot` vs noVNC canvas vs
page screenshot): nothing dims the canvas and it is bit-identical to the framebuffer. The cause was noVNC's
fit-to-window scaling by fractional factors with smoothing, which smears 1-px console glyph strokes (glyph
brightness 114 instead of 170 at 1920x1080). Now: no smoothing when not downscaling, snap to an integer
device-pixel scale when it costs ≤ 15 %, smoothing only when really shrinking; toolbar "1:1 / Fit" toggle
(remembered per browser). Depends on noVNC 1.4 internals (pinned `~1.4.0`). The rest of the "darkness" is the
guest's own grey-on-black text console (not changed: it would alter reproduced customer guests).
`e2e/console.js` checks framebuffer vs canvas vs page at several viewports / DPRs.

## 2. Lab groups

> **Status: v1 done** (2026-10). Isolated network + EL router (dnsmasq + nftables, AlmaLinux 9/10) +
> members with fixed MACs/static leases/DNS names + DNS records (A, CNAME, wildcard), live updates through
> the guest agent, start/stop ordering, delete (keep or delete disks), DB rebuild from libvirt metadata,
> Groups UI (topology, members, network & DNS, router config, export), `vmmanager_group`, `e2e/groups.js`.
> Not yet: VLANs (accepted in the spec, rejected with "not supported yet"), VyOS
> flavour, groups without uplink (the EL router installs its packages at first boot), snapshots, templates,
> export with disks, per-group autostart, `group_id` on `vmmanager_vm`.
>
> **Remote access (WireGuard) — ✅ done** (2026-10): `router.wireguard` ("road warrior"): wg0 on the EL router
> (key pair generated on the router), tunnel /24 from `WG_SUBNET_POOL`, devices = `peers` (public key + tunnel
> IP, added/removed live with `wg syncconf`), dnsmasq answers on the tunnel. The host reaches the router's
> NATed uplink through a UDP relay in the app (`WG_HOST_PORTS`, one port per group): libvirt rejects inbound
> connections to NAT networks and rewrites its rules on every restart, so no DNAT / root. Client config
> (split tunnel: group CIDR + tunnel + router uplink /32 for LBs / kubeadm API) as file + QR code; Remote access
> tab, `vmmanager_wireguard_peer`, `e2e/wireguard.js`, docs/wireguard.md. Not yet: site-to-site between groups
> / hosts (peers with `endpoint` + `allowed_ips` are rendered but have no UI), IPv6, a kernel-path forward for
> high throughput.
>
> **Static reservations for non-members — ✅ done** (2026-10): `spec.dhcp_hosts: [{mac, ip, hostname?}]`
> for machines attached to `vmm-g-<name>` that aren't members (a VM created from the VMs page, an appliance
> booted from an ISO…). Rendered as `dhcp-host=` (+ `host-record=<hostname>.<domain>`), pushed live like any
> spec change, kept in the router metadata and the export. IPs may sit inside the dynamic range (dnsmasq
> never hands a reserved address to another client); router/member IPs, MACs and names, other reservations
> and DNS record names are refused. API `…/groups/{id}/dhcp-hosts[/{mac}]`, `GET …/leases` (each lease marked
> member / reservation / dynamic, with the VM that owns the MAC), `DELETE …/leases/{mac}` (409 while a VM
> with that MAC runs). UI: Network & DNS tab mirrors the Networks DHCP tab ("Make static" on dynamic leases,
> keep the IP or pick another; edit/remove with "release the current lease too"; "Release" on leases of
> stopped VMs). Release = stop dnsmasq, delete the line from its lease file, start it (no `dhcp_release`:
> it needs `dnsmasq-utils` on the router, absent on existing routers, and the push already restarts dnsmasq).
> `vmmanager_group` `dhcp_host` blocks. Note: systemd-networkd ignores the NAK on a plain `networkctl renew`;
> `networkctl reconfigure <if>` or a reboot picks up a changed reservation. `e2e/group-dhcp.js`.
> **Custom members** (done): the full Create VM form adds members too ("Custom VM…" on the Members tab,
> "Lab group" select on the VMs page). `MemberSpec.source` = `cloud_image` | `iso` | `empty` (+ `iso`), so
> ISO installs live in the spec like the rest (router metadata, rebuild, export). ISO/empty members get no
> cloud-init: only the MAC reservation + DNS name, the installed OS must use DHCP. The OpenTofu `member` block
> has `source`, `iso`, `cloud_init` and `user_data` (verified: ISO member got its reserved IP, per-member login
> in the guest, changing one member recreates only that member). Next: PXE boot from the router (dnsmasq
> `dhcp-boot`) would make empty-disk members useful (they boot nothing until an ISO is inserted).
>
> **Router memory** (measured 2026-10-05): default **512 MiB** with a 1 GiB swap file created by cloud-init
> before the first-boot dnf run. Alma 9 / 10: first boot 31–36 s, lowest free 117–120 MiB, swap 57–77 MiB;
> steady state (dnsmasq + nftables + haproxy) ~140–150 MiB used. 384 MiB also boots but swaps ~230 MiB with
> 41 MiB free, so 512 is the reliable value. haproxy is installed on every router (enabled only with load
> balancers), so adding one later needs no dnf run on a live router.

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
  **✅ done (2026-10-09, docs/templates.md):** engine + 7 built-in templates (basic lab, DHCP/DNS records,
  WireGuard, BGP anycast, disconnected registry, kubeadm, OpenShift SNO), wizard, Case guide tab, user templates
  from a group, OpenTofu export. Not yet: a `vmmanager_template` data source, editing user templates in the UI.

> **BGP + Topology view — ✅ done** (2026-10, docs/bgp.md): `router.bgp` rendered as FRR (dynamic neighbors on the
> group CIDR, AS 64512 ← 64513, announce ranges from `BGP_ANNOUNCE_POOL` filtered `le 32`, ECMP over ports), live
> sessions / routes (`GET /groups/{id}/bgp`), BGP tab (settings, sessions, routes, copy-paste FRR / MetalLB configs),
> WireGuard clients route the announce ranges. MetalLB `mode: bgp` for OpenShift (create or day-2 switch). Topology tab
> (group + OpenShift cluster): zones laptop → host → router (role badges) → L2 segment → virtual IPs, tooltips,
> legend, "Follow a packet" stepper, explainers. `vmmanager_group.bgp`, `vmmanager_cluster.openshift.metallb_mode`,
> `e2e/bgp.js`. Not yet: inter-group BGP peering (§2.5), BFD, IPv6, a "BGP + MetalLB" kubeadm recipe automated
> by the app (the BGP tab gives the manifests).

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
  the first control plane). k3s stays on its own network (no router, owner's decision); its `api` record
  points at ctlplane-0 only.
- **Network seam for groups — ✅ done.** Clusters only talk to `ClusterNetwork` (`ensure`, `allocate_ip`,
  `reserve(host, mac, ip)`, `release(mac)`, `publish(ip, names)`, `unpublish(ip)`, `subnet`, `destroy`),
  chosen in one place: `cluster_service.network_for(cluster)`. v1 is `LibvirtClusterNetwork`: an owned NAT
  network `vmm-k-<cluster>` (or an existing one) whose dnsmasq serves the reservations (`net.update`
  DHCP host) and `<dns><host>` records. To put clusters in a lab group: add `GroupClusterNetwork(group)`
  where `ensure` = the group exists and runs, `allocate_ip`/`reserve` = add a member with a fixed MAC/IP
  to the group spec, `publish` = add A records (`api`, `api-int`, nodes, later `*.apps`) to the group's
  DNS records, and re-render + push the router config (`router_service`); `destroy` = nothing (the group
  owns the network). Then `ClusterCreate` gets `group_id`, `network_for` returns the group variant when
  `cluster.group_id` is set, and the router's haproxy can front `api` for 3 control planes (kubeadm/OCP).
  *As built*: cluster nodes are not group members but spec `reservations` (static lease + DNS name; the
  driver creates the VMs) owned by `cluster:<name>`, like their DNS records and the generic
  `load_balancers` entry (`{name, port, backends: [ip:port], mode: tcp}` -> haproxy `listen` section,
  roundrobin, TCP checks; haproxy is installed on every router at first boot but only enabled when the spec
  has load balancers; `haproxy_connect_any` set once). Changes are batched per operation (`commit()` = one
  router push). The router's uplink lease is pinned as a DHCP reservation on the uplink network so the host
  reaches the load balancers on a fixed address. `ClusterCreate.group_id`, or an auto-created group named
  after the cluster (`spec.owner`, deleted with it); a group hosting a cluster can't be deleted.
- **kubeadm — ✅ done** (`KubeadmDriver`, always in a lab group): cloud-init installs containerd.io
  (Docker repo, SystemdCgroup) + kubeadm/kubelet/kubectl from pkgs.k8s.io (pinned minor, default v1.37),
  then the app runs through the guest agent `kubeadm init` (controlPlaneEndpoint `api.<cluster>.<domain>`
  = router haproxy, app-generated token + certificate key, SANs incl. the router uplink IP), Flannel
  (pod CIDR patched in), control-plane joins one at a time, worker joins (token re-created each time).
  Debian 13 1+1 ready in ~3 min; AlmaLinux 10 3+1 with ctlplane-0 off still serves the API through haproxy.
  EL nodes: SELinux permissive (kubeadm docs). Not done: Calico / network policies, upgrades, etcd backups.
- The app fetches the kubeconfig (via the guest agent or SSH) and exposes it as a download.
- Cluster = a **group with a role layout** (`ctlplanes`, `workers`, `type`) + a `clusters` table
  (`kubeconfig`, `version`, `status`). UI: *Clusters* page, kubeconfig download, console of each node.

### 3.3 OpenShift (agent-based installer) — plan (2026-10-05), in progress

Research: kcli (UPI + static pods, SNO bootstrap-in-place) vs the **agent-based installer (ABI)**: ABI wins
(one flow for SNO / compact / HA, no bootstrap VM, Red Hat's documented on-prem path, fits our router DNS +
DHCP reservations + ISO boot). kcli is still the reference for operator recipes (`apps/<name>/`).

**Topology.** Always in a lab group (auto-created per cluster by default), `platform: none`, the router is
the external DNS + LB like customers' UPI setups:
- DNS: `api`, `api-int` -> router LAN IP (SNO: the node IP), `*.apps` -> router LAN IP (wildcard
  `address=`), node names via reservations (PTR from dnsmasq `dhcp-host`/`host-record`).
- haproxy (TCP): 6443 -> masters, 22623 -> masters, 80/443 -> ingress nodes (masters when compact/SNO,
  workers otherwise). Fixed ports => **one OpenShift cluster per group**.
- `SNO` (1 master), `compact` (3 masters, schedulable), `HA` (3 masters + N workers). Nodes: q35,
  host-passthrough, virtio disk 120 GiB (empty, thin) + SATA CD with the agent ISO, boot order disk -> CD
  (empty disk falls through to the ISO, installed disk wins after the reboot), fixed MACs reserved in
  the group, `rootDeviceHints: /dev/vda`. Defaults: SNO 8 vCPU / 24 GiB, masters 8 / 20 GiB, workers 4 / 12 GiB.
  A host budget check (RAM / vCPU / disk) runs before anything is created.

**Versions and binaries.** `GET /openshift/versions?channel=stable-4.20` = upgrade graph API
(`api.openshift.com/api/upgrades_info/v1/graph`, filter on the minor prefix, never hard-code `4.`),
`GET /openshift/channels` lists minors from mirror.openshift.com. Per version, `openshift-install` and `oc`
(`clients/ocp/<ver>/openshift-{install,client}-linux.tar.gz`, sha256sum.txt checked) are cached in
`DATA_DIR/openshift/bin/<ver>/`; the installer's base-ISO cache in `DATA_DIR/openshift/cache`
(`XDG_CACHE_HOME`). Downloads are tasks.

**Pull secret.** Stored once in `DATA_DIR/openshift/pull-secret.json` (0600, never in the DB, never
returned): upload in the UI, or import from a host path (`~/pull-secret.json`). Validated as JSON with `auths`.
A per-cluster SSH key pair is generated (`sshKey`, for `agent-gather` / debug; private key downloadable).

**Install flow** (task `cluster_create`, cancellable):
1. group (create or check) -> reservations + DNS + LB entries (`GroupClusterNetwork`, one router push);
2. binaries (cached); render `install-config.yaml` + `agent-config.yaml` (rendezvousIP = master-0,
   hosts with MAC / role / hostname / root device) + `openshift/*.yaml` manifests for day-1 options
   (operator Subscriptions, SR-IOV MachineConfig, chrony); `openshift-install agent create image` in
   `DATA_DIR/openshift/clusters/<name>/` (0700);
3. upload `agent.x86_64.iso` to the default pool as `<cluster>-agent.iso`; create + start the VMs;
4. phase A: poll the Assisted Service (`http://<rendezvous>:8090/api/assisted-install/v2/clusters`,
   `Watcher-Authorization` token from the install state) from the **router** with guest-exec (the host
   can't route to the group) -> progress %, host stages; phase B: `oc` from the host with a kubeconfig whose
   server is `https://<router uplink IP>:6443` + `tls-server-name: api.<cluster>.<domain>` until
   `clusterversion` is Available and not Progressing (clusteroperators listed in the status);
5. eject + delete the ISO, approve pending CSRs, run the add-ons (below), store kubeconfig + kubeadmin
   password (DB, never in list responses), console URL `https://console-openshift-console.apps.<cluster>.<domain>`.
Start = masters first, approve CSRs until all nodes Ready; stop = ACPI shutdown (warn during the first 24 h).
No "add workers" in v1 (`oc adm node-image create` later).

**Add-ons** (`addons` in the create request, also installable day-2 from the cluster page):
- *Operators*: curated list (LVMS, ODF, LSO, SR-IOV, MetalLB, NMState, OpenShift Virtualization, GitOps,
  Pipelines, ...) + any package name; day-2 picker reads `packagemanifests` live. Install = Namespace (from
  `suggested-namespace`) + OperatorGroup (own namespace unless AllNamespaces-only) + Subscription
  (defaultChannel), wait for the CSV `Succeeded`, then the operator's CR if we know one.
- *Storage*: `none` | `lvms` (SNO / any size: one extra 100 GiB disk per node, `LVMCluster` on
  `/dev/vdb`, default StorageClass `lvms-vg1`) | `odf` (>= 3 nodes: LSO `LocalVolumeSet` + ODF
  `StorageCluster` `resourceProfile: lean`, +8 vCPU / +24 GiB per storage node, RBD default class).
  Extra disks get a serial (`/dev/disk/by-id/virtio-<serial>`).
- *SR-IOV*: each node gets vIOMMU + N igb NICs (on the group network), the SR-IOV operator with
  `DEV_MODE=TRUE` (igb is not in `supported-nic-ids`), `SriovOperatorConfig` (`disableDrain` on SNO /
  compact), a MachineConfig `intel_iommu=on iommu=pt` (day 1), a sample `SriovNetworkNodePolicy`
  (netdevice, 4 VFs) + `SriovNetwork` with whereabouts.
- *MetalLB*: operator + `MetalLB` CR + L2 `IPAddressPool` carved out of the group CIDR (the router's DHCP
  range is shrunk to keep it free) + `L2Advertisement`. **Scenario "MetalLB L2 lab"**: a demo `hello`
  Deployment + `Service type=LoadBalancer`, DNS record `hello.<domain>` -> its external IP, a diagram in
  the UI (laptop -WireGuard-> router -> L2/ARP -> announcing node -> pods), checks (curl from the router,
  which node announces), failover demo (stop the announcing node). BGP mode: ✅ done (2026-10, `metallb.mode`,
  docs/bgp.md).

**API**: `ClusterCreate.type = "openshift"` + `openshift: {version, channel, topology, storage, operators[],
sriov{enabled, nics, vfs}, metallb{enabled, addresses, demo}}`; `/openshift/{pull-secret, channels, versions,
catalog}`; `/clusters/{id}/{kubeadmin, ssh-key, operators, packagemanifests, addons/...}`.
UI: create modal (version picker, topology, sizes, storage, operators, SR-IOV, MetalLB) with a resource
summary; cluster page: install progress (Assisted stages, cluster operators), console link + kubeadmin,
Operators tab, MetalLB lab tab with the diagram. OpenTofu: `vmmanager_cluster` `type = "openshift"` +
`openshift` block. e2e: `openshift.js` (SNO + LVMS + MetalLB demo; long). Disconnected: ✅ done (2026-10,
`openshift.disconnected`: mirror registry + oc-mirror on the group router, egress blocked, docs/disconnected.md;
the router keeps its uplink, so no uplink-less group is needed). Not in v1: OKD,
`platform: baremetal` with VIPs, BGP, add workers, upgrades, FIPS.

---

## 4. Suggested order

Done so far (2026-10): all of §1, groups v1 (+ reservations, custom members), k3s and kubeadm clusters.
Next, in order:

1. **OpenShift SNO with the agent-based installer (§3.3)** on a lab group: everything it needs exists
   (router DNS incl. wildcards, generic `load_balancers`, ISO + boot order, fixed MACs/reservations,
   pinned uplink for host access). Pull secret stored encrypted, `openshift-install` cached per version.
   Then compact (3 nodes, haproxy for api/api-int/22623/ingress), then **OKD** for people without a
   subscription, then **disconnected** (mirror registry; needs groups without uplink, see 2).
2. **Groups v2 (§2.5)**: groups without uplink (prebuilt router image or a local package mirror —
   required for disconnected OpenShift), FRR/BGP, WireGuard (incl. between hosts), VLANs, snapshots,
   templates, export with disks, PXE (`dhcp-boot`) for empty-disk members, NIC options in member specs.
3. SR-IOV on OpenShift: run the SR-IOV Network Operator on these VMs (igb, `docs/sriov.md`) and turn the
   steps into a group/cluster option; real VF pools on RHEL lab hosts with SR-IOV NICs.
4. Cross-cutting, before sharing widely: **authentication** (at least a local user + token; the API
   can create VMs on the host), a **CI** job (`scripts/check-python.py`, backend import, `tsc` + build,
   `go vet`, `e2e/smoke.js`), and a per-group **resource budget** check (CPU/RAM/disk) before creating,
   so a lab doesn't starve the host.

### Sources
- kcli: https://github.com/karmab/kcli, https://kcli.readthedocs.io/en/latest/
- VyOS cloud-init: https://docs.vyos.io/en/1.5/automation/cloud-init.html; images: https://downloads.vyos.io/, https://blog.vyos.io/vyos-stream-2025.11
- OpenWrt: https://openwrt.org/docs/guide-developer/uci-defaults, https://openwrt.org/docs/guide-user/installation/openwrt_x86
- OPNsense automation: https://git.nationtech.io/NationTech/harmony/src/branch/master/docs/use-cases/opnsense-vm-integration.md, https://pypi.org/project/opnsense-confgen
- dnsmasq `dhcp_release`: https://man.archlinux.org/man/dhcp_release.1.en
- OpenShift SNO / agent-based installer: https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/installing_on_a_single_node/install-sno-installing-sno, https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/installing_an_on-premise_cluster_with_the_agent-based_installer/installing-with-agent-based-installer
