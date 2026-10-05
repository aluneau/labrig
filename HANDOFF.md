# Handoff: state on 2026-10-05

Read `CLAUDE.md` first (layout, run/verify commands, architecture rules, portability rules), then
`future-features.md` (roadmap and designs; done items are marked). This file covers **where we are and what's next**.

## Status: working and verified

Everything below was tested for real: in headless Chrome against the real UI (`e2e/`), against real
libvirt on this host, and in fresh nested VMs. On 2026-10-05 the whole e2e suite (`smoke lifecycle full
netedit iso kbd devices groups clusters`) passed against the merged `main` on this rig, leaving nothing behind.

| Area | State |
|---|---|
| VMs | create from cloud image (cloud-init: user, password, SSH keys, keyboard layout), ISO, or empty disk; start / ACPI shutdown / reboot / pause / force off / delete (+ disks); details: IPs, disks, NICs, autostart |
| VM devices | CD/DVD insert/eject (live), boot order + "boot from CD next start" (once), disks add / grow / detach (hot-plug; 16 pcie-root-ports on new VMs) |
| Live status | libvirt events + task progress → SSE `/api/v1/events`; UI updates without refresh, pending states ("Shutting down…") until libvirt confirms |
| Console | noVNC in the browser through the `/api/v1/vms/{id}/vnc` WebSocket bridge (VNC stays on localhost); AZERTY verified with keyboard `fr` on Debian 13 and AlmaLinux 10 (and keymap on Alma 9) |
| Storage | pools, volumes, ISO upload / download from URL (progress, cancel), cloud images (Ubuntu 22/24/26, Debian 12/13, Alma 9/10, Rocky 9, CentOS Stream 9/10, custom URL) |
| Networks | create/start/stop/autostart/delete; edit subnet, DHCP range, domain, forward mode (restart); static DHCP reservations applied live, "make static" from a lease; release a lease (helper); raw XML editor |
| libvirt daemon | connect on demand, closed after 5 min idle with no browser; 503 "libvirt is stopped" + Start button everywhere; start/stop from header/Host page (polkit, no sudo); stop modes refuse / shut down VMs first / force |
| Lab groups v1 | isolated network + AlmaLinux router VM (512 MiB + first-boot swap file; dnsmasq DHCP/DNS, nftables NAT, haproxy TCP load balancers on its LAN + reserved uplink address) + members with reserved IPs + DNS records; live updates pushed through the guest agent; start/stop ordering; rebuild from libvirt metadata; `vmmanager_group` |
| Clusters (k3s) | 1 or 3 control planes + workers (Debian 13 / AlmaLinux 9, SELinux enforcing) on an own NAT network `vmm-k-<name>` with reservations + DNS (`api.<name>.<domain>`); ready in ~1–2 min; kubeconfig usable from the host; kubectl views, stop/start, add/remove workers, rebuild from `<vmmc:cluster>` metadata; `vmmanager_cluster` |
| Clusters (kubeadm) | in a lab group (existing, or auto-created and deleted with the cluster): nodes = group reservations owned by the cluster, `api`/`api-int` records → router haproxy over every control plane, kubeconfig server = router uplink address; Debian 13 (1+1 ready in ~3 min) and AlmaLinux 10 (3+1 in ~4.5 min, API keeps answering with ctlplane-0 off); Kubernetes v1.37 (pkgs.k8s.io), containerd.io 2.x, Flannel; add/remove workers, stop/start (router with an auto group), `vmmanager_cluster.group_id`, example `kubeadm`, `e2e/kubeadm.js` |
| OpenTofu | `vmmanager_cloud_image`, `_network` (`dhcp_hosts`), `_vm` (`cloud_init`, `running`, `wait_for_ip`, `mac_address`, `boot_order`, `cdrom`), `_disk`, `_group`, `_cluster`; examples `basic`, `lab`, `devices`, `group`, `k3s` |
| Install / ship | `scripts/setup.sh` (idempotent, `--no-boot`, `--no-service`, `--listen/--port`; installs the privileged helper + polkit rules), `scripts/package.sh`. Verified from the tarball on **AlmaLinux 9** (Py 3.9, SELinux enforcing, modular daemons), **AlmaLinux 10**, **Debian 13** (monolithic libvirtd), and this CachyOS rig |

## This host (CachyOS gaming rig)

- App runs as the `vm-manager` system service (User=aluneau, 127.0.0.1:8000), **not enabled at boot**;
  libvirtd is **not enabled at boot** either (owner's choice). After a reboot:
  `sudo systemctl start libvirtd vm-manager` (or start vm-manager only and press Start in the UI once
  the helper is installed).
- **Pending owner action:** re-run `scripts/setup.sh --no-boot` once, then `sudo systemctl restart vm-manager`.
  It installs `/usr/libexec/vm-manager/helper`, the `org.vmmanager.helper` polkit action and
  `/etc/polkit-1/rules.d/50-vm-manager.rules`. Until then, libvirt start/stop and lease release from the
  UI don't work here, and `e2e/libvirtctl.js` hasn't been run on this rig (it passed in nested Alma 9 / Debian 13).
- Monolithic libvirtd (Arch, Debian) ignores `--timeout` while a network is active, so the daemon stays up
  until Stop is pressed; the modular daemons on EL exit on their own.
- Changes made with the owner's approval: user in `libvirt` group; `qemu-full`, `edk2-ovmf`, `swtpm`,
  `libvirt-python` installed; `default` network active + autostart; ufw rules for `virbr+`
  (DHCP 67/udp, DNS 53, route in/out).
- Old podman containers `vm-manager-backend` / `vm-manager-frontend` (stale code) are stopped with
  restart=no. Can be deleted.
- Kept in the default pool: cloud images Debian 13, AlmaLinux 9, AlmaLinux 10.
- The OpenTofu provider (merged build, all 6 resources) is installed in
  `~/.terraform.d/plugins/registry.opentofu.org/local/vmmanager/0.1.0/`.
- Nested KVM is enabled. Test installs inside VMs created by the app itself.
- Git: local repo, branch `main`, no remote yet. The agent worktrees under `.claude/worktrees/` (ignored)
  and their `worktree-agent-*` branches are merged and can be removed.

## Known limitations / open issues

- **No authentication.** It listens on 127.0.0.1 by default. Needed before `--listen 0.0.0.0` on shared machines.
- An existing VM's CPU/memory can't be edited.
- One-shot boot only applies to starts through the app; reboots from inside the guest keep the CD first
  until power-off (the UI says so).
- Groups: no FRR/BGP, WireGuard, VLANs, VyOS flavour, snapshots, templates or export with disks yet;
  groups without an uplink are rejected (the router installs packages at first boot). The router's first
  boot depends on dnf mirror speed (the e2e test took 106–215 s).
- k3s clusters stay on their own `vmm-k-*` network (owner's decision); with 3 control planes their `api`
  points at ctlplane-0 only. kubeadm: no upgrades, etcd backups or network policies (Flannel); EL nodes run
  SELinux permissive (kubeadm docs) and AlmaLinux 10 nodes install `kernel-modules-extra-$(uname -r)`
  (br_netfilter / xt_conntrack), which needs that exact kernel version on the mirror. Stopping a group that
  hosts a cluster also stops the cluster's nodes. On EL nodes and group routers, `virt_qemu_ga_t` is
  permissive (lab VMs only).
- SQLite reuses the highest deleted id when a table empties (VM ids can restart at 1).
- `docker-compose.yml` / Dockerfiles are legacy and untested with the current code.

## Next steps (owner's priorities)

1. Run `setup.sh --no-boot` on this rig (above), then `e2e/libvirtctl.js`.
2. **OpenShift SNO** with the agent-based installer (§3.3): reuse the group plumbing built for kubeadm
   (`GroupClusterNetwork`, owned records incl. `*.apps`, router `load_balancers` for api/ingress).
3. Groups v2 (§2.5): FRR/BGP, WireGuard, VLANs, snapshots, templates, export; VyOS flavour.
4. Before sharing widely: authentication, a remote + CI job running `e2e/smoke.js` + `go vet`.

## How to verify after changes

```bash
cd backend && venv/bin/python -c "import app.main" && sudo systemctl restart vm-manager
cd frontend && npx tsc --noEmit -p . && CI=true npx react-scripts build
cd e2e && node smoke.js        # + lifecycle full netedit iso kbd devices groups clusters (libvirtctl needs the helper)
cd opentofu_provider && go vet ./... && make install
```
