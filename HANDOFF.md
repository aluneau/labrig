# Handoff: state on 2026-11-01

Read `CLAUDE.md` first. Earlier handoffs are in git history (`git log -p HANDOFF.md`).

## Where we are

- `main` = GitHub `aluneau/labrig`, pushed, CI green. Releases up to **v0.1.6** (Oct 27, fixes only); main = 0.1.7, ready to tag (`git tag -a v0.1.7 -m "VM Manager 0.1.7" && git push origin v0.1.7`), adds:
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
  with cluster nodes, EL guests refusing set-time, cancel mid-run. 2026-11-01: `snapshots-cluster.js` PASS (AlmaLinux 9
  memory revert, clock skew 0 s; group snapshot of a kubeadm cluster: ConfigMap gone after revert, nodes Ready). Cancel
  mid-run still untested.
- **Host resource budget** (docs/budget.md): RAM blocks (409 + `force=true`, `force_budget` in MCP / OpenTofu), vCPU
  and thin disks warn, "Host resources" line in create modals. `budget.js` refusal path verified on a sandbox backend
  with a huge reserve; `groups.js` passed on main. 2026-11-01: `budget-force.js` PASS (group, member, k3s, add worker:
  409 then forced to ready).

- **OpenShift dual stack completed** (docs/ipv6.md): MetalLB BGP over IPv6, BFD on IPv6 sessions (multihop),
  dual stack + platform baremetal (v6 VIPs). Real SNO 4.20.41 BGP+BFD v6 by the agent; `bgp-bfd.js` + `ipv6.js`
  passed on main. Compact baremetal dual stack: installer accepted the config, never installed (RAM).
- **IPv6-only OpenShift** (`openshift.ip_family: ipv6`, disconnected only, platform none, no MetalLB): real
  disconnected SNO 4.20.44 by the agent (`openshift-ipv6only.js`), registry over IPv6 via haproxy on the router,
  topology shows v6-only nodes. On main: refusal checked, `smoke.js` + `ipv6.js` passed; 2026-11-01
  `openshift-ipv6only.js` PASS on main (real install, 67 min). Not done: MetalLB / baremetal VIPs / kubeadm IPv6-only.

## Done 2026-11-01

- **"database is locked" fixed**: creates failed intermittently (task progress UPDATE past the 15 s busy timeout). Cause:
  on this btrfs rig, fsync of a small file stalls up to 7.5 s while VM disks are copied / written, and SQLite's default
  `synchronous=FULL` fsyncs on every commit while holding the write lock. Now `synchronous=NORMAL` (WAL: commits
  don't fsync, still consistent after a crash) + 30 s busy timeout (`database.py`).
- Version bumped to 0.1.7, CI green; the tag (= release) is left to the owner (0.1.6 was tagged on Oct 27 from another checkout).
- Never run for real: 3-node dual stack baremetal OpenShift (not enough RAM), libvirt stop (`libvirtctl.js`, nested
  install only).

## Next candidates (roadmap)

- Clone / export a group with disks (share a reproducer with a colleague on a RHEL lab host).
- Inter-group connectivity (BGP over an interconnect network, or WireGuard between routers / hosts).
- k3s air gap (needs k3s in a lab group), NAT66 / IPv6-only groups, TLS-intercepting proxy, VLANs.

## Notes
- npm/npx aren't in PATH: `node node_modules/typescript/bin/tsc --noEmit -p .`,
  `CI=true node node_modules/react-scripts/bin/react-scripts.js build`.
- libvirt is not started at boot on this rig: e2e against :8000 gives 503 until it's started (Host page or API).
- Scan for secrets before every push (public repo; OKD's `aWQ6cGFzcwo=` placeholder is expected).
