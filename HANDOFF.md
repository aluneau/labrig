# Handoff: state on 2026-10-05 (end of session)

Read `CLAUDE.md` first (layout, run/verify commands, architecture rules, portability rules), then
`future-features.md` ("Status at a glance", designs, §4 next steps). This file covers **where we are and what's next**.

## Status: working and verified

Everything below was tested for real: headless Chrome against the real UI (`e2e/`), real libvirt on this
host, and fresh nested VMs. On 2026-10-05 the whole suite passed against the final `main` on this rig with
nothing left behind: `smoke lifecycle full netedit iso kbd devices nics console groups group-members
group-dhcp clusters kubeadm` (+ `scripts/check-python.py`, backend import, `tsc` + CI build, `go vet`).

| Area | State |
|---|---|
| VMs | create from cloud image (cloud-init: user, password, SSH keys, keyboard layout — applied on Debian *and* EL), ISO, or empty disk; power actions; delete (+ disks) |
| VM devices | CD/DVD insert/eject (live), boot order + "boot from CD next start", disks add / grow / detach (hot-plug) |
| NICs + SR-IOV | add/remove/move NICs live, link up/down; models virtio, e1000e, igb (emulated SR-IOV, 7 VFs), …; virtual IOMMU + guest kernel args; host SR-IOV view + VF count (helper); "SR-IOV VF pool" networks (`hostdev`), verified nested. See `docs/sriov.md` (OpenShift operator settings) |
| Console | noVNC through the `/vms/{id}/vnc` WebSocket bridge; sharp scaling (no smoothing unless shrinking) + 1:1/Fit toggle — the "dark console" fix; AZERTY verified on Debian 13 and AlmaLinux 10 |
| Storage / Networks | pools, volumes, ISOs (upload / download), cloud images; networks with live DHCP reservations, "make static", lease release (helper), raw XML |
| libvirt daemon | connect on demand, closed when idle; 503 "libvirt is stopped" + Start button everywhere; start/stop from the UI (polkit, no sudo) |
| Lab groups | isolated network + AlmaLinux router (512 MiB + first-boot swap; dnsmasq, nftables NAT, haproxy load balancers) + members (cloud image, ISO or empty disk; quick add or full "Custom VM…" form) + DNS records + static reservations / "Make static" / release for non-members; live router pushes; rebuild from libvirt metadata |
| Clusters | **k3s** on its own network (no router, owner's choice); **kubeadm** in a lab group (existing or auto-created), API behind the router's haproxy on its pinned uplink address, 1 or 3 control planes, Kubernetes v1.37 + Flannel; kubeconfig download + copy-paste kubectl commands (bash/zsh/fish) |
| OpenTofu | `vmmanager_cloud_image`, `_network` (incl. `mode = "hostdev"`), `_vm`, `_disk`, `_nic`, `_group` (members with `source`/`iso`/`cloud_init`/`user_data`, `dns_record`, `dhcp_host`), `_cluster` (k3s, kubeadm, `group_id`); examples `basic lab devices group k3s kubeadm sriov`. Installed build = final `main` |
| Install / ship | `scripts/setup.sh`, `scripts/package.sh`. Tarball verified on AlmaLinux 9, AlmaLinux 10 (re-verified at the end: Python 3.12), Debian 13 and this rig |

## This host (CachyOS gaming rig)

- `vm-manager` service (User=aluneau, 127.0.0.1:8000) and libvirtd are **not enabled at boot** (owner's
  choice). After a reboot: `sudo systemctl start libvirtd vm-manager`.
- **Pending owner action:** run `scripts/setup.sh --no-boot` once, then `sudo systemctl restart vm-manager`.
  It installs the root helper `/usr/libexec/vm-manager/helper` (lease release, VF counts) and the polkit
  rules for libvirt start/stop. Until then those UI actions don't work here. Do **not** run `e2e/libvirtctl.js`
  against this host (it stops libvirt): it passed against nested AlmaLinux 9 / Debian 13 installs.
- The rig's Python is **3.14** (lazy annotations): code that imports here can still fail on RHEL 9 / Alma 10.
  Run `scripts/check-python.py` (it caught a real bug in `schemas/group.py` during this session).
- No SR-IOV NIC and no IOMMU on this rig: real VF passthrough is only testable nested (docs/sriov.md).
- Cloud images kept in the default pool: Debian 13, AlmaLinux 9, AlmaLinux 10. The `test` k3s cluster and
  `mylab` group were the owner's; another session was asked by the owner to delete them.
- Git: local repo, `main` only, no remote. Repo-local identity set (owner's name/email). All agent worktrees
  and branches were merged and removed. Old podman containers `vm-manager-backend/frontend` are stale (deletable).
- Changes made earlier with the owner's approval: user in `libvirt` group; `qemu-full`, `edk2-ovmf`, `swtpm`,
  `libvirt-python`; `default` network autostart; ufw rules for `virbr+`. Nested KVM enabled.

## Known limitations / open issues

- **No authentication.** Listens on 127.0.0.1 by default; needed before `--listen 0.0.0.0` on shared machines.
- An existing VM's CPU/memory can't be edited. One-shot boot only applies to starts through the app.
- Groups: no FRR/BGP, VLANs, VyOS, snapshots, templates, export with disks; no group without an
  uplink (the router installs packages at first boot); empty-disk members boot nothing without an ISO (no PXE).
  `missing` groups (network deleted outside the app) are kept as records; they no longer block the name/subnet.
- Clusters: k3s with 3 control planes points `api` at ctlplane-0 only. kubeadm: no upgrades, etcd backups or
  network policies; EL nodes run SELinux permissive; Alma 10 nodes need `kernel-modules-extra-$(uname -r)` on
  the mirror. Stopping a group that hosts a cluster stops its nodes. `virt_qemu_ga_t` is permissive on EL nodes
  and routers (lab VMs only).
- SR-IOV: emulated igb only here (no RDMA/switchdev/offload); OpenShift's operator needs the unsupported-NIC
  settings from docs/sriov.md (not yet run end to end on OpenShift).
- OpenTofu manages groups' `dns_record` / `dhcp_host` lists wholesale: entries added in the UI show as drift.
- SQLite reuses the highest deleted id when a table empties. `docker-compose.yml` / Dockerfiles are legacy.

## How this session worked (for the next one)

Features were built by parallel agents in git worktrees (own port + DB copy + name prefix each, never
touching the owner's VMs or the :8000 service), then merged one by one into `main`, rebuilt, and re-verified
with the whole e2e suite against :8000. Bugs found while verifying merges were fixed on `main`: listing races
(objects deleted mid-request → 404/500), VM create vs list race, keyboard layout never applied on EL (and
cloud-init `error` on Debian), Python < 3.14 import failure, stale "missing" groups blocking names.
Shared scratch dirs got clobbered by agents (`tofu.rc`, a `grp.py` shadowing the stdlib): give each agent
its own scratch dir.

## Next steps (owner's priorities)

1. Owner: `scripts/setup.sh --no-boot` on this rig (above).
2. **OpenShift SNO** with the agent-based installer (future-features §3.3) in a lab group: reuse
   `GroupClusterNetwork`, owned DNS records (incl. `*.apps`), router `load_balancers`, ISO + boot order.
   Then compact 3-node, OKD, disconnected.
3. Groups v2 (§2.5): no-uplink groups (needed for disconnected OpenShift), BGP/VLANs, site-to-site WireGuard, snapshots,
   templates, export, PXE; NIC options (e.g. igb workers) in member/cluster specs for SR-IOV operator labs.
4. Before sharing widely: authentication, a remote + CI (`scripts/check-python.py`, backend import, frontend
   build, `go vet`, `e2e/smoke.js`), per-group resource budget.

## How to verify after changes

```bash
cd backend && venv/bin/python -c "import app.main" && cd .. && scripts/check-python.py
sudo systemctl restart vm-manager
cd frontend && npx tsc --noEmit -p . && CI=true npx react-scripts build
cd e2e && node smoke.js   # then the suite listed in CLAUDE.md (KUBECTL=… for clusters.js / kubeadm.js)
cd opentofu_provider && go vet ./... && make install
```
