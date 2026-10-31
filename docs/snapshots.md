# Snapshots (beginner-friendly)

## The idea in one minute

A **snapshot** saves a machine at one instant so you can come back to it later. Typical use: the lab
reproduces the customer's problem, you take a snapshot, try the customer's fix (or three different ones),
and **revert** between attempts instead of rebuilding the lab.

vm-manager takes **internal qcow2 snapshots**: the snapshot lives inside the VM's disk file, no extra files
and no chain of overlays to manage.

- A **running** VM is saved **with its memory**: reverting puts it back exactly where it was, programs
  running, shells open, Kubernetes pods up. Nothing reboots.
- A **shut-off** VM is saved **disks only**: reverting puts its disks back and leaves it shut off.

A **group snapshot** saves a whole lab group at one instant: router, members and cluster nodes.

```
take:    pause router + members + nodes ──► snapshot each (same name) ──► resume all
revert:  each VM back to the snapshot, paused ──► resume all together ──► set every clock to the host's time
```

## VM snapshots

VMs page → expand a VM → **Snapshots** tab:

- **Take snapshot**: a name (letters, digits, `.`, `_`, `-`) and an optional description. A few seconds for a
  1 GiB VM (the memory is written into the disk file: the VM's RAM size counts).
- **Revert**: the confirmation says what is lost: **everything written to the VM since the snapshot**. Take a
  new snapshot first if you may want the current state back. After a memory revert the guest's clock is
  set to the host's time through the QEMU guest agent.
- **Delete**: removes the snapshot only; the VM's current state is not touched.

API (`/api/v1`, every change runs as a task: `202` + the task, follow it on `/tasks/{id}` or the live events):

```
GET    /vms/{id}/snapshots                    list + whether the VM can have snapshots (reasons in plain words)
POST   /vms/{id}/snapshots                    {"name": "before-fix", "description": "…"}
POST   /vms/{id}/snapshots/{name}/revert
DELETE /vms/{id}/snapshots/{name}
```

Each snapshot shows when it was taken, the state then (`running` / `paused` / `shutoff`), whether the memory
is included, and which one is **current** (the last taken or reverted to).

## Group snapshots

Group page → **Snapshots** tab, same buttons.

- **Take**: every VM of the group is paused (members first, router last), snapshotted with the same name,
  then resumed. If one VM fails, the snapshots already taken are deleted again (all or nothing). The task
  can be cancelled between two VMs. While paused, the guests' clocks stop: they are set to the host's
  time after the resume.
- **Revert**: every VM goes back to the snapshot paused, then they are resumed together (router first) and
  their clocks are set to the host's time. That matters for clusters: OpenShift / kubeadm certificates,
  etcd leases and kubelet heartbeats misbehave with a clock in the past. If a guest can't set its clock (no
  guest agent, or an EL VM whose agent forbids it), the task says so and the lab still works; fix that
  guest's clock by hand (`chronyc makestep`).
- **The group settings come back too**: DNS records, DHCP reservations, WireGuard devices, BGP… changed
  after the snapshot are gone. The router's own copy of the settings (its libvirt metadata) is part of the
  snapshot, and the app reads it back.
- **Refused when machines changed**: a member (or cluster node) added or removed since the snapshot would
  be out of step with the rest, so the revert is refused with the names, e.g. *"added since:
  lab-extra"*. Remove the added member (or take a new snapshot) first. The same holds when a VM was
  re-created since and has no snapshot of that name.
- **Delete**: deletes the snapshot on every VM holding it.

```
GET    /groups/{id}/snapshots                 group snapshots, the VMs holding each one, `revertable` / `problem`
POST   /groups/{id}/snapshots                 {"name": "lab-ok", "description": "…"}
POST   /groups/{id}/snapshots/{name}/revert
DELETE /groups/{id}/snapshots/{name}
```

A group snapshot is simply the same snapshot name on all its VMs: you can also revert one VM of the group on
its own from the VMs page (e.g. a single member). Reverting the **router** alone brings its old settings
back while the app keeps the current ones: use **Router → Apply again** afterwards.

Starting / stopping the group and snapshot operations of the same group don't run at the same time.

## What can't be snapshotted

The Snapshots tab says why, in plain words, and the buttons are disabled:

- **SR-IOV VF NICs or other passed-through devices** (`hostdev`): their state lives in the hardware.
- **UEFI firmware** (NVRAM): internal snapshots don't cover it. VMs created by the app boot BIOS.
- **Raw or non-file disks**: internal snapshots need qcow2 disk files (the app always creates qcow2).

CD-ROMs (install ISOs, the cloud-init seed) are read-only and simply not part of the snapshot.

## Good to know

- **Disk space**: a snapshot keeps the disk blocks changed since, plus the memory of a running VM, inside the
  qcow2 file. Delete the snapshots you no longer need.
- **Deleting a VM** deletes its snapshots. With "keep disks", the snapshots are removed from the kept disk
  files first, so no hidden data stays in them.
- **Cluster records** in the app (node list, add-ons, MetalLB settings) are not part of a snapshot: since the
  machines must be the same, they still match. A cluster upgrade, add-on install or node change done
  after the snapshot is undone inside the cluster, while the app's view of it may show the newer state.
- **Networks** are not part of a snapshot (libvirt networks have no state to save).
- **MCP / AI assistants** reach these endpoints through `api_request` (listed by `api_describe`).
- **OpenTofu**: no resource. Snapshots are something you do (take, revert), not a desired state to describe.

## Under the hood

libvirt `virDomainSnapshotCreateXML` with `<memory snapshot='internal'/>` when the VM is active (QEMU writes
the RAM into the qcow2), `<memory snapshot='no'/>` when it is off; revert with `virDomainRevertToSnapshot`
(`REVERT_RUNNING`, or `REVERT_PAUSED` for groups; `REVERT_FORCE` only when libvirt asks for it); clocks with
`virDomainSetTime` (guest agent `guest-set-time`; the clock is read back, since qemu-ga also runs
`hwclock -w`, which fails in guests without it). Code: `services/snapshot_service.py`, libvirt calls in
`libvirt_client.py` (`create_snapshot`, `revert_snapshot`, `snapshot_blockers`, `set_guest_time`), endpoints
`api/v1/endpoints/snapshots.py`, UI `components/snapshots/SnapshotsPanel.tsx`, test `e2e/snapshots.js`.
