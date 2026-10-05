# Handoff: state on 2026-10-04 (end of session)

Read `CLAUDE.md` first (layout, run/verify commands, architecture rules, portability rules), then
`future-features.md` (roadmap and designs). This file covers **where we are and what's next**.

## Status: working and verified

Everything below was tested for real: in headless Chrome against the real UI (`e2e/`), against real
libvirt on this host, and in fresh nested VMs.

| Area | State |
|---|---|
| VMs | create from cloud image (cloud-init: user, password, SSH keys, keyboard layout), ISO, or empty disk; start / ACPI shutdown / reboot / pause / force off / delete (+ disks); details: IPs, disks, NICs, autostart |
| Live status | libvirt events + task progress → SSE `/api/v1/events`; UI updates without refresh, pending states ("Shutting down…") until libvirt confirms |
| Console | noVNC in the browser through the `/api/v1/vms/{id}/vnc` WebSocket bridge (VNC stays on localhost); AZERTY verified with keyboard `fr` |
| Storage | pools, volumes, ISO upload / download from URL (progress, cancel), cloud images (Ubuntu 22/24/26, Debian 12/13, Alma 9/10, Rocky 9, CentOS Stream 9/10, custom URL) |
| Networks | create/start/stop/autostart/delete; edit subnet, DHCP range, domain, forward mode (restart); static DHCP reservations applied live, "make static" from a lease; raw XML editor |
| OpenTofu | Go provider `opentofu_provider/` (`vmmanager_cloud_image`, `vmmanager_network` with `dhcp_hosts`, `vmmanager_vm` with `cloud_init`, `running`, `wait_for_ip`, `mac_address`); apply / idempotent plan / in-place update / drift detection / destroy verified; examples `examples/opentofu/basic` (my-vm) and `lab` |
| Clusters (k3s) | 1 or 3 control planes + workers (Debian 13 / AlmaLinux 9, SELinux enforcing) on an own NAT network `vmm-k-<name>` with reservations + DNS (`api.<name>.<domain>`); ready in ~1–2 min; kubeconfig usable from the host; kubectl views, stop/start, add/remove workers, rebuild from `<vmmc:cluster>` metadata; `vmmanager_cluster` (workers/running in place), `e2e/clusters.js` |
| Install / ship | `scripts/setup.sh` (idempotent, `--no-boot`, `--no-service`, `--listen/--port`), `scripts/package.sh` (tarball with prebuilt UI). Verified from the tarball on **AlmaLinux 9** (Py 3.9, SELinux enforcing, firewalld), **AlmaLinux 10**, **Debian 13** (ufw, 192.168.122 conflict handled), and on this CachyOS rig. Each test ran a nested VM that got DHCP, SSH and internet |

## This host (CachyOS gaming rig)

- App runs as the `vm-manager` system service (User=aluneau, 127.0.0.1:8000), **not enabled at boot**;
  libvirtd is **not enabled at boot** either (owner's choice). After a reboot:
  `sudo systemctl start libvirtd vm-manager`.
- Changes made with the owner's approval: user in `libvirt` group; `qemu-full`, `edk2-ovmf`, `swtpm`,
  `libvirt-python` installed; `default` network active + autostart; ufw rules for `virbr+`
  (DHCP 67/udp, DNS 53, route in/out).
- Old podman containers `vm-manager-backend` / `vm-manager-frontend` (stale code) are stopped with
  restart=no. Can be deleted.
- Kept in the default pool: cloud images Debian 13, AlmaLinux 9, AlmaLinux 10.
- The OpenTofu provider is installed in `~/.terraform.d/plugins/registry.opentofu.org/local/vmmanager/0.1.0/`.
- Nested KVM is enabled. Test installs inside VMs created by the app itself.

## Known limitations / open issues

- **No authentication.** It listens on 127.0.0.1 by default. Needed before `--listen 0.0.0.0` on shared machines.
- The libvirt reconnect watchdog (`main.py` `keep_connected`) keeps libvirtd alive while the app runs.
  That conflicts with "start/stop libvirt on demand" (design in future-features §1.5).
- The project is **not a git repository** yet (a `.gitignore` is ready). The owner wants to ship it, so
  `git init` + a remote is the obvious first step (ask before doing it).
- An existing VM's CPU/memory can't be edited. (Disks, CD-ROM media and boot order can: §1.2–1.4 done.)
- `docker-compose.yml` / Dockerfiles are legacy and untested with the current code.
- Two VMs the owner made (`test`, `debian-test`) disappeared during the session, apparently deleted by
  the owner while testing (tests only touch `e2e-*` / `tofu-*`). Unconfirmed.

## Next steps (owner's priorities)

1. Small features (future-features §1): start/stop libvirt from the UI (+ drop the keepalive), ISO
   attach/eject + "boot from ISO next time", add disks, release DHCP lease.
2. **Lab groups** (§2): isolated network + router VM generated from a group spec. Recommended router:
   minimal EL cloud image + dnsmasq/nftables/FRR/WireGuard via cloud-init; VyOS as an alternative flavour.
   Plus a `vmmanager_group` OpenTofu resource.
3. **Kubernetes, then OpenShift** on groups (§3): k3s/kubeadm first, then OpenShift SNO with the
   agent-based installer (router VM serves api/api-int/*.apps DNS + LB); kcli as optional escape hatch.
4. Before sharing widely: authentication, `git init`, a CI job running `e2e/smoke.js` + `go vet`.

## How to verify after changes

```bash
cd backend && venv/bin/python -c "import app.main" && sudo systemctl restart vm-manager
cd frontend && npx tsc --noEmit -p . && CI=true npx react-scripts build
cd e2e && node smoke.js && node full.js        # + lifecycle.js netedit.js iso.js kbd.js
cd opentofu_provider && go vet ./... && make install
```
