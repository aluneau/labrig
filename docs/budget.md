# Host resource budget

The host is often a desktop used for other things too. Before creating anything, the app checks that it fits
next to what already runs, so a lab doesn't make the host swap or the kernel OOM-kill a program.

Checked on: VM create (also "Custom VM" in a group), lab group create, member add (and members added by a group
spec PUT), k3s / kubeadm cluster create (nodes + the router of an auto-created group), k3s / kubeadm "Add worker".
OpenShift keeps its own stricter RAM rule (`openshift_installer._check_budget`: no overcommit at all, an
OOM-killed master corrupts etcd); templates show an estimate in their preview.

## Rules

| What | Rule | Result |
|---|---|---|
| Memory | new VMs' memory > `MemAvailable` - `BUDGET_MEMORY_RESERVE_MIB` - what running VMs may still grow into (max memory - resident) - VMs the operation starts (e.g. the stopped router of the group a kubeadm cluster joins) | **refused** |
| Memory | uses more than 80 % of that | warning |
| vCPU | vCPUs of running + new VMs above `BUDGET_VCPU_OVERCOMMIT` × host CPUs | warning |
| Disk | cloud image copies (written in full at creation) > free space of the storage pool | **refused** |
| Disk | full size of the new (thin qcow2) disks > free space | warning |

New VMs count even when created stopped: they are made to be run.

## Settings (backend/.env or /etc/vm-manager/vm-manager.env)

| Variable | Default | |
|---|---|---|
| `BUDGET_MEMORY_RESERVE_MIB` | 2048 | RAM kept for the host |
| `BUDGET_VCPU_OVERCOMMIT` | 4.0 | vCPU / CPU ratio above which a warning is shown |
| `BUDGET_ENFORCE` | true | false = never refuse, only warn |

## Refusal and override

A refused create answers **HTTP 409** with the numbers in plain words, e.g.
`Not enough host resources: Memory: needs 24 GiB, 11 GiB available (30 GiB free, running VMs may still take
17 GiB; 2 GiB kept for the host): stop something or shrink the VMs. Pass force=true to create anyway.`
Nothing is created. `?force=true` on the same request creates it anyway (writes are admin-only).

- UI: every create form shows a "Host resources" line (needs / available, warnings in yellow, problems in red);
  when it would be refused, Create stays disabled until "Create anyway" is ticked.
- API: `POST /api/v1/hosts/budget` is the dry run (body: one of `vm`, `group`, `group_id` + `members`, `cluster`,
  `cluster_id` + `count`, or explicit `items`), same code as the creates.
- MCP: the tools get the 409 message as their error; `create_vm`, `create_group`, `add_group_member`,
  `create_cluster` take `force_budget`.
- OpenTofu: the apply fails with the message; `force_budget = true` on `vmmanager_vm`, `vmmanager_group`,
  `vmmanager_cluster` (also for members / workers added later).

Code: `backend/app/services/budget.py`; e2e: `e2e/budget.js` (backend with a huge `BUDGET_MEMORY_RESERVE_MIB`).
