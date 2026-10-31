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

## Done 2026-10-31 (merged, verified on :8000)

- **Snapshots** (docs/snapshots.md): VM + group, internal qcow2, memory when running, group revert = resume router
  first + clock sync, refused when members changed. `snapshots.js` + `smoke.js` passed on main. Not tested: groups
  with cluster nodes, EL guests refusing set-time, cancel mid-run.
- **Host resource budget** (docs/budget.md): RAM blocks (409 + `force=true`, `force_budget` in MCP / OpenTofu), vCPU
  and thin disks warn, "Host resources" line in create modals. `budget.js` refusal path verified on a sandbox backend
  with a huge reserve; `groups.js` passed on main. Not run for real: forced group / cluster create, member add, add
  worker.

- **OpenShift dual stack completed** (docs/ipv6.md): MetalLB BGP over IPv6, BFD on IPv6 sessions (multihop),
  dual stack + platform baremetal (v6 VIPs). Real SNO 4.20.41 BGP+BFD v6 by the agent; `bgp-bfd.js` + `ipv6.js`
  passed on main. Compact baremetal dual stack: installer accepted the config, never installed (RAM).
- **IPv6-only OpenShift** (`openshift.ip_family: ipv6`, disconnected only, platform none, no MetalLB): real
  disconnected SNO 4.20.44 by the agent (`openshift-ipv6only.js`), registry over IPv6 via haproxy on the router,
  topology shows v6-only nodes. On main: refusal checked, `smoke.js` + `ipv6.js` passed; no OpenShift install
  re-run on the merged code. Not done: MetalLB / baremetal VIPs / kubeadm IPv6-only.

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
