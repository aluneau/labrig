# Handoff: state on 2026-11-14

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
  mid-run: `snapshots-cancel.js` PASS.
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
- Compact dual stack `platform: baremetal` OpenShift installed for real: `openshift-ipv6-baremetal.js` PASS (4.20.44, 56 min:
  v4+v6 VIPs in Infrastructure, nodes v4+v6, AAAA, API /readyz + console on the IPv6 VIPs). `snapshots-cancel.js` PASS.
- Review of the 0.1.7 code (worth its size; ~50 lines trimmed in budget / snapshots, budget dry run lost the unused
  `items` / `starting` inputs and `note`). Fixed: bare-metal Linux installs (admin sudo, `%post` done callback ->
  installed / ISO deleted, one-line key / repo URL; unit tests only, no BMC hardware here), AAAA for day-2 workers in
  dual stack (not run on a real cluster), topology shows IPv6 addresses in dual stack.
- Rust rewrite feasibility (agent study, not in the repo): feasible, 30-50 person-weeks; not recommended now.
- **Upgrades everywhere** (UI / API / MCP / OpenTofu): `openshift-day2.js` PASS (add / remove worker, upgrade 4.18.54 ->
  4.18.55). MCP `cluster_upgrade` tool; OpenTofu `version` change on OpenShift = in-place upgrade (verified: SNO 4.18.54
  -> 4.18.55 in 55 min, plan clean after), channel stored in the spec. Fixed on the way: node-image create retries on a
  restarting API (EOF right after install), one agent ISO build at a time (parallel creates broke the shared installer
  cache), OpenTofu `cloud_image_id` unknown after an OpenShift create.
- Never run for real: libvirt stop (`libvirtctl.js`, nested install only), bare-metal Linux install on hardware.

## 2026-11-02: rig froze ~9 min under 2 clusters (investigated, nothing fixed yet)

- Symptom: desktop frozen ~23:30 -> 23:39. Running: test-debian, mynetwork (rtr + node1), test-k3s (3 VMs, from
  ~23:20), then test-kubeadm (rtr + 3) created at 23:35-23:36 = 10 VMs, 2 clusters bootstrapping at once. Discord
  went unresponsive and crashed (SIGTRAP) at 23:38; recovered once test-k3s was deleted (23:38:38).
- Not the cause: RAM (~20 GB of VMs / 94 GB, no OOM, memory PSI ~0), CPU (PSI full = 0), GPU (no NVRM/Xid), kernel
  (no hung task / lockup; journald kept logging every 5 s). No atop/sysstat, so no per-minute history.
- Cause: **I/O saturation of `sda`** (Crucial BX500 SATA, DRAM-less) that holds `/`, `/home` AND
  `/var/lib/libvirt/images`. Since boot (49 h): IO PSI `full` total 5245 s, ~1.5 TB written, 5.2 M flushes, mean
  write latency ~23 ms. etcd fsyncs (guest flush -> qemu -> btrfs -> SSD) + image pulls (~3 GB per kubeadm node
  in 14 min) fill its SLC cache; every desktop process touching disk then blocks. Same root cause as the
  "database is locked" fix of 2026-11-01 (fsync stalls on this rig).
- Aggravating: images dir is `nodatacow` but inside `@`, which snapper snapshots (54 snapshots) -> COW after each
  snapshot anyway; `sda` scheduler is `mq-deadline`. Dirty limits are already small (256 MB / 64 MB). NVMe
  (`nvme0n1`, P3) is Windows/NTFS, not available.
- Proposed, not done (owner to confirm):
  1. App: optional disk cache mode for throwaway lab VMs, `cache='unsafe'` (ignores guest flushes). Every disk goes
     through `data_disk_xml()` (`backend/app/domain_xml.py:125`); would need a group/cluster setting + API / MCP /
     OpenTofu field, off by default.
  2. Host: `/var/lib/libvirt/images` in its own btrfs subvolume `@libvirt` (excluded from snapper), VMs stopped
     during the move.
  3. Host: lower `IOWeight` for `machine.slice` (or `IOWriteBandwidthMax` on `/dev/sda`) so VMs can't starve the desktop.
  4. Host: BFQ on `sda` (udev rule).
  5. Long term: a dedicated NVMe for VM images.
- To verify after changes: recreate the same load (k3s + kubeadm clusters at once) while watching
  `/proc/pressure/io` and `iostat -x sda 5`; the desktop must stay responsive.

## 2026-11-14: OpenShift 5.0 (UNCOMMITTED: whole working tree, owner to confirm the commit)

- Asked: deploy OCP 5.0. `stable-5.0` (5.0.0, 5.0.1) comes from the same update graph and the same
  `mirror.openshift.com/.../ocp/<ver>` client dirs (same archive names) -> channels / versions / binaries needed no
  change. Add-ons take the packagemanifest default channel -> no change. `redhat-operator-index:v5.0` exists.
- Uncommitted edits found at start (earlier session) and kept: 5.x accepted in `MirrorRequest.openshift_version`,
  `GroupRegistry` VERSION_RE, disconnected check in `openshift_installer` (`4.` / `5.`), templates `acm-hub` /
  `openshift-sno` offer stable-5.0 (default still stable-4.20), provider descriptions, docs/openshift.md line.
  Also in the tree, unrelated: ClusterDetailPage MetalLB BGP/BFD label, the 2026-11-02 freeze notes below.
- Fixed: `ocp/stable` now = 5.0.1, whose client dir has **no oc-mirror** (nor the release payload): the router's
  download 404'd for EVERY disconnected install, 4.x too. `registry_router.OC_MIRROR_URL` = space-separated list
  (stable, then stable-4.22), script loops over it (`OCM_URL` quoted). oc-mirror 4.22 dry-run of 5.0.1 = 198
  images, OK; used for real in the runs below.
- Fixed: disconnected 5.0.1 hung on `openshift-samples` (CO without status; operator deleted its Config and kept
  running registry.redhat.io connection tests; ClusterVersion blocked 90+ min -> 150 min timeout). 4.x bootstraps
  Removed by itself. Now `_manifests()` adds `openshift/99-vmm-samples-removed.yaml` (samples Config
  `managementState: Removed`) for every disconnected install (docs/disconnected.md step 4). Proven live first by
  creating the Config by hand (CO Available ~2 min later).
- Verified: `CHANNEL=stable-5.0 node openshift-disconnected.js` PASS clean (no manual step) in 71 min: SNO 5.0.1
  (RHCOS 10 base ISO `coreos10-x86_64.iso`, node v1.36.5), ClusterVersion Available, samples Removed, LVMS +
  MetalLB 5.0.0 from `cs-redhat-operator-index-v5-0`, hello demo, pod / node internet blocked. Cluster deleted,
  e2e token revoked, UI rebuilt, service restarted. tsc, go build, check-python, check-templates OK.
- Not run on 5.0: connected install, compact / ha, platform baremetal, IPv6, OKD 5, ACM, day 2 workers,
  upgrades 4.22 -> 5.0 (fast-5.0 / candidate-5.0 graphs contain 4.22.x, so the path exists).
- **Open question to the owner** ("make it work on a disconnected env"): case 1 = cluster air-gapped, host +
  router online (works, verified above) vs case 2 = the vm-manager host itself has no internet (not supported for
  any version: installer / oc / oc-mirror / mirror-registry / release all downloaded). Case 2 would be an offline
  bundle (built on a connected machine: binaries + oc-mirror mirror-to-disk archive + mirror-registry) imported
  into the router registry (disk-to-mirror), install with no outbound call. Wait for the answer before building.
- Rig: kernel 7.3.4 was installed while 7.3.3 ran -> no modules for the running kernel (bridge, sch_htb, nft
  nat...): lab groups failed with "Le paquetage n'est pas installé" / "qdisc kind is unknown" / network
  'default' not active. Worked around with the 7.3.3 package from the pacman cache bind-mounted read-only at
  /usr/lib/modules/7.3.3-1-cachyos + libvirtd restart. **Owner is rebooting**: afterwards
  `sudo rmdir /usr/lib/modules/7.3.3-1-cachyos`, then `sudo systemctl start vm-manager` (not at boot; libvirt
  starts on demand). Symptom to recognise next time: any of those three errors right after a kernel update.

## Next candidates (roadmap)

- Clone / export a group with disks (share a reproducer with a colleague on a RHEL lab host).
- Inter-group connectivity (BGP over an interconnect network, or WireGuard between routers / hosts).
- k3s air gap (needs k3s in a lab group), NAT66 / IPv6-only groups, TLS-intercepting proxy, VLANs.

## Notes
- npm/npx aren't in PATH: `node node_modules/typescript/bin/tsc --noEmit -p .`,
  `CI=true node node_modules/react-scripts/bin/react-scripts.js build`.
- libvirt is not started at boot on this rig: e2e against :8000 gives 503 until it's started (Host page or API).
- Scan for secrets before every push (public repo; OKD's `aWQ6cGFzcwo=` placeholder is expected).
