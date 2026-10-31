"""Snapshots of VMs and lab groups (docs/snapshots.md)

Internal qcow2 snapshots: the disks, plus the memory state when the VM runs, so a revert resumes the
guest exactly where it was. A group snapshot pauses every VM of the group (router, members, cluster
nodes), snapshots each one with the same name and resumes them: the lab is captured at one instant.
A group revert puts every VM back paused, resumes them together, then sets their clocks to the host's
(OpenShift / kubeadm certificates, etcd leases and kubelets don't like a clock in the past). The group
spec is read back from the reverted router's metadata, which holds the spec of that time.

All long operations run as tasks; libvirt calls go through libvirt_client.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import libvirt
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.libvirt_client import libvirt_client
from app.models import Group, Task, VM
from app.schemas import TaskCreate
from app.schemas.group import GroupSpec
from app.schemas.snapshot import SnapshotCreate
from app.services.task_service import task_service

logger = logging.getLogger(__name__)

# Group task types that must not overlap (start / stop / snapshot operations of one group)
GROUP_TASK_TYPES = ["group_start", "group_stop", "group_snapshot", "group_snapshot_revert", "group_snapshot_delete"]


def _when(epoch: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(epoch, timezone.utc) if epoch else None


def _public(snap: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in snap.items() if k != "group_meta"}
    out["created"] = _when(snap["created"])
    return out


def _error_text(e: Exception) -> str:
    if isinstance(e, libvirt.libvirtError):
        return e.get_error_message() or str(e)
    return str(e)


class SnapshotService:

    def __init__(self):
        self._busy: set = set()
        self._busy_lock = threading.Lock()

    # One snapshot operation at a time per VM

    def _claim(self, names: Iterable[str]) -> List[str]:
        names = list(names)
        with self._busy_lock:
            busy = [n for n in names if n in self._busy]
            if busy:
                raise ValueError(f"A snapshot operation is already running on {', '.join(busy)}: wait for it to finish")
            self._busy.update(names)
        return names

    def _release(self, names: Iterable[str]) -> None:
        with self._busy_lock:
            self._busy.difference_update(names)

    def _start(self, db: Session, names: List[str], task: TaskCreate, func, *args) -> Task:
        """Run func(db, task, *args) as a task holding the VMs' snapshot lock"""
        self._claim(names)

        def body(session: Session, t: Task, *a):
            try:
                return func(session, t, *a)
            finally:
                self._release(names)
        try:
            return task_service.start(db, task, body, *args)
        except Exception:
            self._release(names)
            raise

    @staticmethod
    def _check_supported(names: List[str], prefix: bool) -> None:
        reasons = []
        for name in names:
            for reason in libvirt_client.snapshot_blockers(name):
                reasons.append(f"{name}: {reason}" if prefix else reason)
        if reasons:
            raise ValueError("Snapshots are not possible because " + "; ".join(reasons))

    @staticmethod
    def _sync_clocks(names: List[str]) -> Dict[str, str]:
        """Set each guest's clock to the host's, in parallel: {vm: why it failed}"""
        if not names:
            return {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: (n, libvirt_client.set_guest_time(n)), names))
        return {n: err for n, err in results if err}

    # VM snapshots

    def list_vm(self, vm: VM) -> Dict[str, Any]:
        snaps = libvirt_client.list_snapshots(vm.name)
        reasons = libvirt_client.snapshot_blockers(vm.name)
        return {"snapshots": [_public(s) for s in snaps], "supported": not reasons, "reasons": reasons}

    def create_vm(self, db: Session, vm: VM, data: SnapshotCreate) -> Task:
        self._check_supported([vm.name], prefix=False)
        if libvirt_client.has_snapshot(vm.name, data.name):
            raise ValueError(f"{vm.name} already has a snapshot named '{data.name}'")
        return self._start(
            db, [vm.name],
            TaskCreate(name=f"Snapshot {vm.name} ({data.name})", type="vm_snapshot", target_type="vm",
                       target_id=vm.id, target_name=vm.name),
            self._create_vm_task, vm.name, data.name, data.description)

    @staticmethod
    def _create_vm_task(db: Session, task: Task, name: str, snap: str, description: Optional[str]) -> Dict[str, Any]:
        result = libvirt_client.create_snapshot(name, snap, description)
        return {"vm": name, "snapshot": snap, "memory": result["memory"], "state": result["state"]}

    def revert_vm(self, db: Session, vm: VM, snap_name: str) -> Task:
        if not libvirt_client.has_snapshot(vm.name, snap_name):
            raise LookupError(f"{vm.name} has no snapshot named '{snap_name}'")
        self._check_supported([vm.name], prefix=False)
        return self._start(
            db, [vm.name],
            TaskCreate(name=f"Revert {vm.name} to {snap_name}", type="vm_snapshot_revert", target_type="vm",
                       target_id=vm.id, target_name=vm.name),
            self._revert_vm_task, vm.name, snap_name)

    def _revert_vm_task(self, db: Session, task: Task, name: str, snap_name: str) -> Dict[str, Any]:
        snap = next((s for s in libvirt_client.list_snapshots(name) if s["name"] == snap_name), None)
        if snap is None:
            raise LookupError(f"{name} has no snapshot named '{snap_name}'")
        # memory snapshots come back running (a group snapshot was taken paused: that pause is ours)
        libvirt_client.revert_snapshot(name, snap_name, running=snap["memory"])
        task_service.update_progress(db, task.id, 80)
        clock = self._sync_clocks([name]) if snap["memory"] else {}
        return {"vm": name, "snapshot": snap_name, "running": snap["memory"], "clock_errors": clock}

    def delete_vm(self, db: Session, vm: VM, snap_name: str) -> Task:
        if not libvirt_client.has_snapshot(vm.name, snap_name):
            raise LookupError(f"{vm.name} has no snapshot named '{snap_name}'")
        return self._start(
            db, [vm.name],
            TaskCreate(name=f"Delete snapshot {snap_name} of {vm.name}", type="vm_snapshot_delete",
                       target_type="vm", target_id=vm.id, target_name=vm.name),
            self._delete_vm_task, vm.name, snap_name)

    @staticmethod
    def _delete_vm_task(db: Session, task: Task, name: str, snap_name: str) -> Dict[str, Any]:
        libvirt_client.delete_snapshot(name, snap_name)
        return {"vm": name, "snapshot": snap_name}

    # Group snapshots

    @staticmethod
    def _spec_vms(spec: GroupSpec) -> List[str]:
        """Every VM of a group spec: router, members, reserved hosts (cluster nodes)"""
        from app.services.group_service import member_vm_name, router_vm_name
        return ([router_vm_name(spec.name)] + [member_vm_name(spec.name, m.name) for m in spec.members]
                + [r.name for r in spec.reservations])

    def _group_vms(self, spec: GroupSpec) -> List[str]:
        """The group's VMs that exist in libvirt (reservations may be planned nodes not created yet)"""
        existing = {vm["name"] for vm in libvirt_client.list_vms()}
        return [n for n in self._spec_vms(spec) if n in existing]

    def _check_group(self, db: Session, group: Group) -> GroupSpec:
        if group.status in ("creating", "deleting", "missing"):
            raise ValueError(f"Group is {group.status}")
        busy = db.query(Task).filter(Task.target_type == "group", Task.target_id == group.id,
                                     Task.type.in_(GROUP_TASK_TYPES),
                                     Task.status.in_(["pending", "running"])).first()
        if busy is not None:
            raise ValueError(f"'{busy.name}' is still running: wait for it to finish")
        return GroupSpec.model_validate(group.spec)

    def _group_snapshots(self, spec: GroupSpec, vms: List[str]) -> List[Dict[str, Any]]:
        """Group snapshots = snapshots of the router, with the VMs holding the same name"""
        from app.services.group_service import router_vm_name
        rtr = router_vm_name(spec.name)
        per_vm = {}
        for name in vms:
            try:
                per_vm[name] = {s["name"]: s for s in libvirt_client.list_snapshots(name)}
            except libvirt.libvirtError:
                per_vm[name] = {}
        now = set(self._spec_vms(spec))
        result = []
        for snap in per_vm.get(rtr, {}).values():
            holders = [n for n in vms if snap["name"] in per_vm[n]]
            problem = None
            then_spec = None
            meta = snap.get("group_meta") or {}
            if meta.get("spec"):
                try:
                    then_spec = GroupSpec.model_validate(meta["spec"])
                except ValidationError:
                    then_spec = None
            if then_spec is None:
                problem = "The router's snapshot holds no group settings: revert the VMs one by one instead"
            else:
                then = set(self._spec_vms(then_spec))
                added, removed = sorted(now - then), sorted(then - now)
                missing = [n for n in vms if n not in holders]
                if added or removed:
                    parts = []
                    if added:
                        parts.append(f"added since: {', '.join(added)}")
                    if removed:
                        parts.append(f"removed since: {', '.join(removed)}")
                    problem = ("The group's machines changed since this snapshot (" + "; ".join(parts)
                               + "). Reverting would leave them out of step: remove the added ones or "
                               "take a new snapshot")
                elif missing:
                    problem = (f"{', '.join(missing)} has no snapshot named '{snap['name']}' "
                               "(re-created since?): revert the VMs one by one instead")
            result.append({
                "name": snap["name"], "description": snap["description"], "created": _when(snap["created"]),
                "vms": [{"vm": n, "state": per_vm[n][snap["name"]]["state"],
                         "memory": per_vm[n][snap["name"]]["memory"]} for n in holders],
                "revertable": problem is None, "problem": problem, "_spec": then_spec,
            })
        return sorted(result, key=lambda s: (s["created"] or datetime.min.replace(tzinfo=timezone.utc), s["name"]))

    def list_group(self, group: Group) -> Dict[str, Any]:
        spec = GroupSpec.model_validate(group.spec)
        vms = self._group_vms(spec)
        reasons = []
        for name in vms:
            reasons += [f"{name}: {r}" for r in libvirt_client.snapshot_blockers(name)]
        snaps = [{k: v for k, v in s.items() if k != "_spec"} for s in self._group_snapshots(spec, vms)]
        return {"snapshots": snaps, "vms": vms, "supported": not reasons, "reasons": reasons}

    def create_group(self, db: Session, group: Group, data: SnapshotCreate) -> Task:
        spec = self._check_group(db, group)
        planned = self._spec_vms(spec)
        vms = self._group_vms(spec)
        absent = [n for n in planned if n not in vms]
        if absent:
            raise ValueError(f"{', '.join(absent)} doesn't exist: a group snapshot needs every machine of the group")
        self._check_supported(vms, prefix=True)
        taken = [n for n in vms if libvirt_client.has_snapshot(n, data.name)]
        if taken:
            raise ValueError(f"{', '.join(taken)} already has a snapshot named '{data.name}'")
        return self._start(
            db, vms,
            TaskCreate(name=f"Snapshot lab group {group.name} ({data.name})", type="group_snapshot",
                       target_type="group", target_id=group.id, target_name=group.name),
            self._create_group_task, group.id, vms, data.name, data.description)

    def _create_group_task(self, db: Session, task: Task, group_id: int, vms: List[str], snap: str,
                           description: Optional[str]) -> Dict[str, Any]:
        paused: List[str] = []
        created: List[str] = []
        clock: Dict[str, str] = {}
        try:
            # Members first, router last: the lab freezes as one (nothing talks to a paused router)
            for name in reversed(vms):
                if (libvirt_client.get_vm(name) or {}).get("state") == "running":
                    libvirt_client.suspend_vm(name)
                    paused.append(name)
            task_service.update_progress(db, task.id, 5)
            for i, name in enumerate(vms):
                if task_service.is_cancelled(task.id):
                    raise RuntimeError("Cancelled")
                libvirt_client.create_snapshot(name, snap, description)
                created.append(name)
                task_service.update_progress(db, task.id, 5 + 85 * (i + 1) // len(vms))
        except Exception:
            for name in created:  # all or nothing
                try:
                    libvirt_client.delete_snapshot(name, snap)
                except libvirt.libvirtError as e:
                    logger.warning(f"Could not roll back snapshot {snap} of {name}: {e}")
            raise
        finally:
            self._resume(paused, vms)
            clock = self._sync_clocks(paused)  # they were paused: their clocks are behind
        self._publish_group(db, group_id)
        return {"snapshot": snap, "vms": vms, "memory": paused, "clock_errors": clock}

    @staticmethod
    def _resume(names: List[str], order: List[str]) -> None:
        """Resume paused VMs, router (first of `order`) first; a failure doesn't stop the others"""
        for name in sorted(names, key=order.index):
            try:
                if (libvirt_client.get_vm(name) or {}).get("state") == "paused":
                    libvirt_client.resume_vm(name)
            except libvirt.libvirtError as e:
                logger.warning(f"Could not resume {name}: {e}")

    def _find_group_snapshot(self, spec: GroupSpec, snap_name: str) -> Dict[str, Any]:
        vms = self._group_vms(spec)
        snap = next((s for s in self._group_snapshots(spec, vms) if s["name"] == snap_name), None)
        if snap is None:
            raise LookupError(f"Group {spec.name} has no snapshot named '{snap_name}'")
        return snap

    def revert_group(self, db: Session, group: Group, snap_name: str) -> Task:
        spec = self._check_group(db, group)
        snap = self._find_group_snapshot(spec, snap_name)
        if not snap["revertable"]:
            raise ValueError(snap["problem"])
        vms = [v["vm"] for v in snap["vms"]]
        self._check_supported(vms, prefix=True)
        return self._start(
            db, vms,
            TaskCreate(name=f"Revert lab group {group.name} to {snap_name}", type="group_snapshot_revert",
                       target_type="group", target_id=group.id, target_name=group.name),
            self._revert_group_task, group.id, snap_name)

    def _revert_group_task(self, db: Session, task: Task, group_id: int, snap_name: str) -> Dict[str, Any]:
        group = db.query(Group).filter(Group.id == group_id).first()
        spec = GroupSpec.model_validate(group.spec)
        snap = self._find_group_snapshot(spec, snap_name)
        if not snap["revertable"]:
            raise ValueError(snap["problem"])
        if task_service.is_cancelled(task.id):
            raise RuntimeError("Cancelled")
        vms = [v["vm"] for v in snap["vms"]]  # router first
        with_memory = [v["vm"] for v in snap["vms"] if v["memory"]]
        reverted: List[str] = []
        # No cancel from here on: a half-reverted lab is worse than either state
        try:
            # Each VM back paused (memory snapshots) or shut off (disk-only ones)...
            for i, name in enumerate(vms):
                libvirt_client.revert_snapshot(name, snap_name, paused=name in with_memory)
                reverted.append(name)
                task_service.update_progress(db, task.id, 5 + 75 * (i + 1) // len(vms))
        except Exception as e:
            self._resume([n for n in reverted if n in with_memory], vms)
            left = [n for n in vms if n not in reverted]
            raise RuntimeError(f"Reverting {left[0]} failed ({_error_text(e)}); reverted: "
                               f"{', '.join(reverted) or 'none'}; not reverted: {', '.join(left)}")
        # ...then all of them resumed together, router first
        self._resume(with_memory, vms)
        task_service.update_progress(db, task.id, 85)
        clock = self._sync_clocks(with_memory)
        task_service.update_progress(db, task.id, 95)
        self._restore_spec(db, group, snap["_spec"])
        return {"snapshot": snap_name, "vms": vms, "running": with_memory, "clock_errors": clock}

    @staticmethod
    def _restore_spec(db: Session, group: Group, spec: GroupSpec) -> None:
        """The DB spec follows the reverted router (its metadata holds the spec of the snapshot's time)"""
        from app.services import group_service as gs
        from app.services import wireguard_service as wgs
        group.spec = spec.model_dump(mode="json")
        group.config_applied, group.config_error = True, None
        db.commit()
        gs.group_service._sync_member_rows(db, group, spec)
        wgs.reconcile(db)
        gs._bmc_reconcile(db)
        gs.group_service._publish(group, "updated")

    def delete_group(self, db: Session, group: Group, snap_name: str) -> Task:
        spec = self._check_group(db, group)
        snap = self._find_group_snapshot(spec, snap_name)
        vms = [v["vm"] for v in snap["vms"]]
        return self._start(
            db, vms,
            TaskCreate(name=f"Delete snapshot {snap_name} of lab group {group.name}", type="group_snapshot_delete",
                       target_type="group", target_id=group.id, target_name=group.name),
            self._delete_group_task, group.id, vms, snap_name)

    def _delete_group_task(self, db: Session, task: Task, group_id: int, vms: List[str], snap_name: str) -> Dict[str, Any]:
        errors = {}
        # router last: the group snapshot stays listed until every VM's one is gone
        for i, name in enumerate(reversed(vms)):
            try:
                libvirt_client.delete_snapshot(name, snap_name)
            except libvirt.libvirtError as e:
                errors[name] = _error_text(e)
            task_service.update_progress(db, task.id, 100 * (i + 1) // len(vms))
        if errors:
            raise RuntimeError("Could not delete the snapshot of " + "; ".join(f"{n} ({e})" for n, e in errors.items()))
        self._publish_group(db, group_id)
        return {"snapshot": snap_name, "vms": vms}

    @staticmethod
    def _publish_group(db: Session, group_id: int) -> None:
        from app.services.group_service import group_service
        group = db.query(Group).filter(Group.id == group_id).first()
        if group is not None:
            group_service._publish(group, "updated")


snapshot_service = SnapshotService()
