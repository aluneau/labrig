# Handoff: state on 2026-10-12 (evening, end of session)

Read `CLAUDE.md` first (layout, verify commands, architecture rules incl. the **ACM** and **Emulated BMCs**
paragraphs), then `docs/openshift.md` "ACM hub" and "Spoke machines (BMC) and provisioning a spoke".
Earlier handoffs are in git history (`git log -p HANDOFF.md`).

## Where we are

- `main` = GitHub `aluneau/labrig`, release **v0.1.3** published (stable channel), CI + release green.
  The rig runs `main` in checkout mode (`vm-manager` service restarted on it). No `e2e-acm-*` VMs/networks left.
- 0.1.2: update UI (masthead "Update available", Host page Update now; hidden on git checkouts).
- 0.1.3: ACM (below) + OpenShift day-2 MetalLB demo fix.

## ACM: what is verified and what is NOT

| Part | State |
|---|---|
| ACM hub add-on (`openshift.acm`, Operators tab, ACM tab) | **Verified for real**: SNO 4.20.40 (32 GiB / 12 vCPU, LVMS), MultiClusterHub Running, ACM 2.17.3. Measured Basic requests ~2.8 vCPU / 8.9 GiB. |
| Import / detach (`/clusters/{hub}/acm/managed`) | **Verified for real**: kubeadm (own group, auto-import-secret, UI) and k3s on `default` (import manifests, API) both JOINED/AVAILABLE; detach clean. k3s on its own NAT network refused (plain words). |
| UI relations (list labels, "Managed by" banner, "Import into ACM hub") | **Verified** by `e2e/acm.js` (screenshots in the agent worktree, see below). |
| Emulated BMC (sushy-tools per group, `bmc: true` members, BMC tab) | **Partly**: emulator checked **from the host only** (401 without creds, only the group's spoke listed, 404 for other VMs). sushy-tools 2.1.0 works on Python 3.9. Never reached **from the hub**. |
| Spoke provisioning (`/clusters/{hub}/acm/spokes`, spokes = cluster rows with `spec.openshift.provisioner`) | **NOT verified**: no spoke ever installed. Code + `SPOKE=1` e2e path exist, shipped in 0.1.3 untested. |
| OpenTofu `vmmanager_acm_managed_cluster`, `vmmanager_acm_spoke`, member `bmc` | Builds, example validates (`tofu fmt`/validate); not applied for real. |

### The firewall blocker is resolved
The hub's metal3/Ironic must reach the emulator on the uplink gateway `192.168.122.1:<BMC_HOST_PORTS 8180-8199>`, and
ufw dropped VM -> host TCP. **Done on this rig on 2026-10-12** (`ufw status` shows it):

```
sudo ufw allow in on virbr+ to any port 8180:8199 proto tcp comment 'vm-manager BMC (Redfish emulator)'
```

The session has **passwordless sudo** on this rig: host-side fixes like this can be applied directly (say so to the
owner) instead of waiting. New hosts get the rule from `scripts/setup.sh` (default `--bmc-ports 8180-8199`, ufw or
firewalld zone `libvirt`, `--bmc-ports none` to skip); release installs get it on their next update (the updater
re-runs setup.sh with the same `--bmc-ports`).

## Next session (owner said: do it tomorrow; needs ~2–3 h and ~50–60 GiB free RAM)

1. Check from a group router (rule already added): `curl -sk -o /dev/null -w '%{http_code}' http://192.168.122.1:8180/redfish/v1/Systems` -> 401
   (plain http unless the emulator got a cert; the group's port is in `GET /groups/{id}/bmc`).
2. Run with a fresh token: `cd e2e && VMM_TOKEN=… SPOKE=1 KEEP=1 node acm.js` (hub SNO + BMC machine + spoke;
   `REUSE=1` reuses an existing hub). Watch: BareMetalHost `provisioned`/agent registered via virtual media, assisted
   install progress, spoke row in Clusters with kubeconfig / logs / MCP `oc`, delete through the hub (machine stays).
   Likely weak spots (unverified code): Ironic image server ports 6180/6183 forwarded by the hub router + emulator
   redirect (commit 50f724b), virtual media download from the host (getaddrinfo patch -> lab zones via router),
   `ensure_cim` AgentServiceConfig storage on LVMS, BMH `bootMode legacy`.
3. Fix what breaks, then OpenTofu: apply `examples/opentofu/acm` once (hub + import + spoke), destroy.
4. Until verified: consider marking spoke provisioning **experimental** in docs/openshift.md + ACM tab (offered,
   not done). Then secret-scan, push, release 0.1.4.
5. Clean up `e2e-acm-*` / `tofu-acm-*` at the end (delete clusters through the API: owned groups go with them).

## Notes
- The ACM agent's worktree is still at `.claude/worktrees/agent-a03663a218382d2da` (branch merged; screenshots in
  its `e2e/screenshots/acm-*.png`). Safe to remove with `git worktree remove` once no longer useful.
- npm/npx aren't in PATH: `node node_modules/typescript/bin/tsc --noEmit -p .`,
  `CI=true node node_modules/react-scripts/bin/react-scripts.js build`.
- Pushing needs `gh` logged in (token was refreshed today); scan before every push (memory: public repo).
