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
                      router_cases (split DNS, proxy, MTU render helpers), router_ipv6 (dual stack render helpers) + ipv6_service
                      (IPv6 /64 assignment, members' network-config, DHCPv6 leases), bgp_service (announce ranges, vtysh status), topology_service (GET /groups/{id}/topology),
                      registry_service (router mirror registry: setup, oc-mirror runs, ensure_mirrored), registry_router
                      (router-side script), registry_images + registry_client (copy / upload / list / delete images),
                      cluster (k3s, kubeadm; cluster_drivers per type, cluster_network = Libvirt / Group node network),
                      k8s_mirror (disconnected kubeadm: images to mirror, containerd hosts.toml)
  api/v1/endpoints/   vms (+ WebSocket /vms/{id}/vnc bridge), storage, networks, hosts, tasks, events (SSE), groups, clusters
                      template_service (customer-case templates: load/validate, render, create, save group as template)
  templates/          built-in customer-case templates (<id>.yaml, docs/templates.md)
  api/v1/endpoints/   vms (+ WebSocket /vms/{id}/vnc bridge), storage, networks, hosts, tasks, events (SSE), groups, clusters, templates
  schemas/ models/    Pydantic API schemas / SQLAlchemy models
frontend/src/
  services/api.ts     typed API client (+ vncUrl)       types/index.ts  API types (keep in sync with backend schemas)
  hooks/              useEvents (one EventSource, useLiveEvents), usePolling, useVmPower (pending states)
  pages/              Dashboard, VMs, Console (noVNC), Storage, Networks, NetworkDetail, Groups, GroupDetail, Templates, TemplateWizard, Clusters, ClusterDetail, Host, Tasks
  components/         common/, layout/, vms/CreateVMModal + VmDevices, groups/CreateGroupModal, clusters/CreateClusterModal, console/VncConsole
docs/sriov.md         SR-IOV labs (igb emulation, vIOMMU, VF pools, OpenShift operator settings)
docs/openshift.md     OpenShift (agent-based installer): topologies, add-ons, MetalLB L2 lab, reaching the console
docs/wireguard.md     lab remote access: enable, devices, laptop steps (nmcli import), troubleshooting
docs/bgp.md           BGP on the group router (FRR), MetalLB BGP mode, beginner-friendly
docs/disconnected.md  egress switch + mirror registry on the router (mirror-registry, oc-mirror v2, own images)
docs/ipv6.md          dual stack groups: RA + DHCPv6 reservations, AAAA, IPv6 egress drop/reject, WireGuard + BGP over IPv6
docs/router-cases.md  split DNS zones, proxy-only egress (squid), MTU / narrow hop / PMTUD black hole; templates in backend/app/templates
docs/templates.md     customer-case templates: file format, placeholders, API, adding one
opentofu_provider/    Go provider (terraform-plugin-framework): vmmanager_cloud_image, _network, _vm, _disk, _nic, _group, _wireguard_peer, _cluster, _sriov_pf
examples/opentofu/    lab (network with DHCP reservations + 2 Debian VMs), devices (disk, ISO, boot order), group (lab group), disconnected (registry + egress), disconnected-kubeadm, router-cases (split DNS, proxy, MTU), ipv6 (dual stack group), sriov-pool, k3s, kubeadm (clusters)
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
  A restart marks running tasks failed (`mark_interrupted`); then `cluster_service.recover_interrupted` resumes an
  OpenShift create whose nodes booted (the install carries on in them: `openshift_installer.resume` = follow it, eject,
  add-ons), sets other interrupted creates to `error` and interrupted node add/remove back to `ready`.
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
  applies at the next cold start. Host SR-IOV state is read from sysfs + `ip -j -d link` (`sriov_service`: readiness
  checks with fix commands per distro, VF MAC/VLAN/trust/IOMMU group). Changes go through the helper (v3):
  `sriov-set-numvfs`, `sriov-vf-options` (trust/spoofchk on every VF of a PF), `sriov-persist` (PF by PCI address in
  `/etc/vm-manager/sriov.conf` + `vm-manager-sriov.service` running `helper sriov-restore` at boot). VF pools: `vlan`
  on the network / NIC (`<vlan><tag>`, libvirt sets it with the MAC through the PF); `vm_service._start_vm` /
  `add_nic` run `sriov_service.check_pool` first (plain-words: no free VF, shared IOMMU group, PF gone). This rig has
  no IOMMU: test VF pools nested (docs/sriov.md §3, `e2e/sriov-real.js` with HOST_SH).
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
  **BFD**: `router.bgp.bfd` (multiplier 3, 200 ms) = `bfdd=yes` + profile `vmm` + `neighbor … bfd profile vmm`; turning it
  on/off restarts FRR (FRR 8.5's frr-reload mangled the config). Status `show bfd peers json` (`bfd_peers`,
  `sessions[].bfd_status`). Measured failover: 27 s without BFD (hold time), 0.6 s with.
  **MetalLB shared code** `services/metallb.py` (config/demo manifests, pools, live checks) used by OpenShift's add-on
  (`AddonRunner(MetalLBClient)`, oc) and **kubeadm** (`kubeadm_metallb`: upstream v0.16.1 FRR mode applied with kubectl
  through the guest agent, `spec.kubeadm.metallb` {enabled, mode, bfd, demo, pool, service_ip, state}); `GET/PUT
  /clusters/{id}/metallb` for both types; `metallb.bfd` adds a BFDProfile (and BFD on the router). kubeadm control planes
  are excluded from LB announcements (label), so next hops = workers.
- **Disconnected labs** (docs/disconnected.md): `router.egress` (`mode` open|blocked, `allow` CIDRs) = nft forward rules on
  the router (blocked: LAN -> anything but lab / WG / BGP ranges / allow is rejected; `ct status dnat` accepted = podman
  published ports), live. `router.registry` (`enabled`, `port`, `disk_gb`, `memory_mb`, `vcpus`; assigned `hostname`,
  read back `ca_pem`): the router gets `effective_memory/vcpu()` and a disk with serial `vmm-registry` (xfs at
  /var/lib/vmm-registry, podman graphroot there too); `registry_service.ensure_ready` restarts the router once when its
  live RAM is short, pushes `registry_router` script + env + credentials (`DATA_DIR/groups/<g>/registry-auth.json`, never
  in spec/API except `/registry/credentials`) and runs the detached `vmm-registry-setup` unit (mirror-registry download
  + install on the router, own CA). Quay `SERVER_HOSTNAME` = `<uplink_ip>:<port>` (token realm reachable from host, lab
  and WireGuard); repos public on push. oc-mirror v2 runs per request hash in detached `vmm-mirror-<id>` units (up to 8 passes: quay.io's CDN drops
  ~1 large blob per release pass, 2 of 3 passes in a real SNO run; re-runs skip copied images), polled
  via guest-exec (survives app restarts; done runs only read back); the pull secret goes to `/run` (tmpfs) for the run
  only. Spec PUTs without `egress`/`registry` keep the stored ones (`model_fields_set`). Own images: skopeo copy on the
  router, archive uploads spooled to disk and pushed from the host by `registry_client` (registry v2 API, no tools).
- **Router cases** (docs/router-cases.md; `services/router_cases.py` = render helpers called by router_service, kept
  separate): `router.dns.zones` (split DNS: `server=/zone/ip`, a server may be a member name resolved at render;
  + `stop_rebind`/`no_negcache`/`cache_size`); egress `mode: proxy` = the blocked forward rules + squid on the router
  (`router.egress.proxy`: port, `allow_domains` dstdomain, `connect_ports`, basic auth hashed on the router with
  `openssl passwd -apr1`, `dns_nameservers 127.0.0.1`; dnf-installed on first use, stopped otherwise); cloud-image
  members created in proxy mode get the proxy env via cloud-init (`member_env`; `bootcmd` writes apt/dnf proxy before
  packages) — raw `user_data` members don't. `network.mtu` = libvirt `<mtu>` (saved def, next group start) + router
  LAN + DHCP option 26 (running Debian members need `networkctl reconfigure`); `router.path` = MTU of both router
  NICs (live `ip link` + NM profile by MAC; uplink reset via a marker file), `drop_frag_needed` (nft output chain),
  `clamp_mss` (`maxseg size set rt mtu`). Spec PUTs without `network`/`path`/`proxy`/dns zones keep the stored ones.
- **IPv6 / dual stack** (docs/ipv6.md; `router_ipv6` = render hooks, `ipv6_service` = assignment): `network.ipv6`
  (`enabled`, `prefix` = a /64 of `IPV6_ULA_POOL` assigned like WG subnets, `egress` reject|drop). Addresses are
  derived, never stored: `spec.ip6_of(ipv4)` = same host number in decimal digits (`.21` -> `<prefix>::21`, router
  `::1`). Stateful DHCPv6 (not SLAAC: AAAA records need known addresses): `dhcp-host=mac,v4,[v6],name`, `enable-ra`,
  `ra-param=*,…`, dynamic range `::dc:0-ffff`. dnsmasq ignores router solicitations with only `listen-address`: the
  push writes `interface=<lan>` (found by MAC) to `/etc/dnsmasq.d/vmm-ipv6-iface.conf` and sets the LAN address with
  `nmcli` (live on/off). Members get a network-config with dhcp6 + accept-ra. nft `table ip6 vmm_group6`: egress
  blocked/proxy reject in forward, `egress: drop` = prerouting drop (no IPv6 uplink: without it the kernel answers "no
  route" at once). FRR: peer group `LAB6` + `address-family ipv6 unicast`, IPv6 announce ranges (`lab6` /64) `le
  128`, `prefer-global`; members need `disable-connected-check` (DHCPv6 /128). WireGuard `subnet6`. Cross-family
  `overlaps()` is wrong in `ipaddress`: use `nets_overlap`. No IPv6 internet, clusters stay IPv4.
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
- **Disconnected OpenShift** (docs/disconnected.md, `openshift.disconnected`, create only): owned group change
  `registry.enabled` -> `registry_service.ensure_mirrored(request, task, window)` (release + add-on packages: deps
  from `redhat-operator-index` `/configs` extracted on the host, `olm.package.required` + ODF's runtime
  `odf-dependencies`; + `DEMO_IMAGE`) -> egress `blocked` in the `_allocate` push (previous mode in
  `openshift.egress_before`, restored on delete from a shared group) -> install-config `pullSecret` = registry auths
  only, `imageDigestSources` with a 2nd `<uplink_ip>:8443` mirror (the host-side `agent create image` can't resolve
  `registry.<domain>`; its oc calls use `--insecure=true --icsp-file`), CA in `additionalTrustBundle` (Always).
  Results (incl. registry auth) in the install dir `mirror.json` 0600. After install: oc-mirror cluster resources,
  `OperatorHub.disableAllDefaultSources`, CatalogSource READY; `AddonRunner.sources` maps redhat-operators -> the
  mirrored CatalogSource. Day-2 add-ons mirror first with the union of the cluster's operators (one filtered catalog
  image per index: a smaller request would drop packages).
- **Disconnected kubeadm** (docs/disconnected.md "Kubernetes (kubeadm)", `k8s_mirror.py`, `disconnected` +
  `mirror_images`, create only; k3s not: no router): registry enabled with `REGISTRY_SIZES` (4 GiB) -> on the router
  `resolve_kubeadm` (patch from stable-<minor>.txt, `kubeadm config images list`, Flannel manifest, base64 into
  `spec.mirror.flannel`: nodes can't download it) -> `registry_images.copy_list` (router script `copylist`, skopeo
  --all) to one Quay namespace per upstream (`k8s/ docker/ ghcr/ quay/`, others host with `-`). Egress `blocked` +
  owned `router.egress.exempt` per new node (nft `ip saddr … accept`) in `_add_nodes`; `ops.prereqs_done` drops the
  exemptions before kubeadm pulls anything (packages = "golden image"). Nodes: `hosts.toml` per registry
  (`override_path`, CA), containerd `config_path` + sandbox image, no `images pull` in prereqs, packages pinned to the
  mirrored patch. Day 2: `POST /clusters/{id}/mirror` (new registry -> hosts.toml pushed via guest-exec). Delete from a
  shared group restores `spec.mirror.egress_before`. Quay 401 on a missing repo = "not mirrored" (ImagePullBackOff).
- **Templates** (docs/templates.md): `backend/app/templates/*.yaml` (built-in, read-only) + `DATA_DIR/templates/*.yaml`
  (user, "Save as template" on a group). Loaded on every request; a bad file is reported in `GET /templates` `errors`,
  never fatal (`scripts/check-templates.py` validates offline: parse, placeholders, render with sample params through
  GroupSpec / ClusterCreate). Plain `{{param}}` substitution (exact placeholder keeps the type), built-ins `{{group}}`
  `{{domain}}` `{{ip:N}}` `{{<cidr param>:N}}`, `_if:` drops a mapping; `cidr: auto` = first free /24 of
  `TEMPLATE_SUBNET_POOL`. Render = preview + host checks (`group_service.normalize` dry run) + estimate vs free RAM +
  OpenTofu HCL; create = `group_service.create_group` with `spec.template` (id, case, params, rendered guide: kept
  by `_keep_owned`, shown on the group's "Case guide" tab) + a `template_cluster` task that waits for the group and
  creates the cluster (kubeadm / openshift; `image:` slug -> cloud_image_id).
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
node templates.js                                               # templates: gallery, wizard (YAML edit, OpenTofu, guide), Basic lab boots, Case guide tab, save-as-template round trip
scripts/check-templates.py                                      # validate template files offline
node bgp.js                                                     # BGP: FRR members, ECMP, filter, WireGuard client VM (created), Topology tab shots, failover
node ipv6.js                                                    # dual stack: DHCPv6/RA, AAAA, EL lease, BGP over IPv6, egress drop/reject/blocked, live toggle (REUSE=1 KEEP=1)
node bgp-bfd.js                                                 # BFD: failover timing without / with BFD (lib-router.js measures on the router)
node kubeadm-metallb.js                                         # kubeadm + MetalLB BGP/BFD + demo, failover, switch to L2 (REUSE=1 KEEP=1)
node disconnected-kubeadm.js                                    # air-gapped kubeadm: mirrored pod Running, un-mirrored ImagePullBackOff, no internet, day-2 mirror (~30 min)
node router-cases.js                                            # split DNS via a member resolver, proxy-only egress (407/403, member env), PMTUD black hole + MSS clamp, MTU
HOST_SH="ssh l1" PF=eth2 VM_NAME=… node sriov-real.js           # VF pools on an SR-IOV host (nested EL L1): checks, VF options, persistence, VLANs
node libvirtctl.js       # STOPS libvirt: only against a nested install (ssh -L tunnel), never this host
cd opentofu_provider && make install && cd ../examples/opentofu/lab && tofu init && tofu apply
```

e2e tests need the server on `BASE_URL` (default http://localhost:8000), Chrome at `CHROME_PATH`, and a
ready Debian 13 cloud image. They create and delete real VMs/networks named `e2e-*`. Screenshots go to
`e2e/screenshots/`. Never touch VMs that aren't named `e2e-*`/`tofu-*` in tests: the user runs their own VMs here.
