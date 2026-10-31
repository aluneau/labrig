# Handoff: state on 2026-10-31

Read `CLAUDE.md` first. Earlier handoffs are in git history (`git log -p HANDOFF.md`).

## Where we are

- `main` = GitHub `aluneau/labrig`, pushed, CI green. Releases up to **v0.1.5**; `main` (0.1.6-dev) adds since then:
  OKD + platform baremetal (VIPs), OpenShift day 2 (add/remove workers, upgrades), dual stack kubeadm / OpenShift,
  bare-metal unattended Linux installs + BMC media fixes (pushed Oct 27–28 from another checkout, merged 2026-10-31).
- 2026-10-31 fixes: a multi-line f-string in `acm_spokes.py` broke Python < 3.12 (CI had been red since Oct 28);
  libvirt start from the app failed on systemd 262 ("Failed to enqueue jobs: Access denied": one `systemctl` call
  with several units = one polkit check without the unit name) -> one call per unit. Start verified on the rig;
  stop not exercised here (would stop the owner's libvirt) — `e2e/libvirtctl.js` on a nested install covers it.
- ACM spokes on emulated BMCs: verified for real (acm.js 2026-10-15, gitops.js 2026-10-16).

## In progress (worktree agents, 2026-10-31)

| Feature | Plan | Branch / port |
|---|---|---|
| Snapshots (VM + group, internal qcow2, revert + clock sync) | future-features.md §2.5 | worktree agent, :8301 |
| Host resource budget before create (RAM / vCPU / pool disk, 409 + force) | future-features.md §4.4 | worktree agent, :8302 |

Merge one at a time into main, rebuild UI, restart `vm-manager`, re-run `smoke.js` + the feature's e2e.

## Next candidates (roadmap)

- Clone / export a group with disks (share a reproducer with a colleague on a RHEL lab host).
- Inter-group connectivity (BGP over an interconnect network, or WireGuard between routers / hosts).
- k3s air gap (needs k3s in a lab group), NAT66 / IPv6-only groups, TLS-intercepting proxy, VLANs.
- Release 0.1.6 once the above is merged and verified.

## Notes
- npm/npx aren't in PATH: `node node_modules/typescript/bin/tsc --noEmit -p .`,
  `CI=true node node_modules/react-scripts/bin/react-scripts.js build`.
- libvirt is not started at boot on this rig: e2e against :8000 gives 503 until it's started (Host page or API).
- Many merged agent worktrees remain under `.claude/worktrees/` (`git worktree list`); remove with
  `git worktree remove` when not needed.
- Scan for secrets before every push (public repo; OKD's `aWQ6cGFzcwo=` placeholder is expected).
