"""Snapshot endpoints (docs/snapshots.md): /vms/{id}/snapshots and /groups/{id}/snapshots

vm_router is mounted under /vms before the vms router (its POST /{vm_id}/{action} would catch
POST /{vm_id}/snapshots). Creating, reverting and deleting run as tasks (202 + the task).
"""
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import Task
from app.schemas.snapshot import SNAPSHOT_NAME, GroupSnapshotList, SnapshotCreate, VMSnapshotList
from app.services.group_service import group_service
from app.services.snapshot_service import snapshot_service
from app.services.vm_service import vm_service

vm_router = APIRouter()
group_router = APIRouter()

SnapName = Path(..., pattern=SNAPSHOT_NAME)


def _run(func, *args):
    try:
        return func(*args)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _vm(db: Session, vm_id: int):
    vm = vm_service.get_vm(db, vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    return vm


def _group(db: Session, group_id: int):
    group = group_service.get_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


@vm_router.get("/{vm_id}/snapshots", response_model=VMSnapshotList)
def list_vm_snapshots(vm_id: int, db: Session = Depends(get_db)):
    """The VM's snapshots (oldest first), and whether it can have any (`reasons` in plain words if not)"""
    return snapshot_service.list_vm(_vm(db, vm_id))


@vm_router.post("/{vm_id}/snapshots", response_model=Task, status_code=202)
def create_vm_snapshot(vm_id: int, data: SnapshotCreate, db: Session = Depends(get_db)):
    """Snapshot the VM: its disks, plus its memory when it runs (a revert then resumes it where it was)"""
    return _run(snapshot_service.create_vm, db, _vm(db, vm_id), data)


@vm_router.post("/{vm_id}/snapshots/{name}/revert", response_model=Task, status_code=202)
def revert_vm_snapshot(vm_id: int, name: str = SnapName, db: Session = Depends(get_db)):
    """Revert the VM to a snapshot: everything written since is lost. Memory snapshots come back running
    (clock set to the host's through the guest agent), disk-only ones shut off."""
    return _run(snapshot_service.revert_vm, db, _vm(db, vm_id), name)


@vm_router.delete("/{vm_id}/snapshots/{name}", response_model=Task, status_code=202)
def delete_vm_snapshot(vm_id: int, name: str = SnapName, db: Session = Depends(get_db)):
    """Delete a snapshot (the VM's current state is not touched)"""
    return _run(snapshot_service.delete_vm, db, _vm(db, vm_id), name)


@group_router.get("/{group_id}/snapshots", response_model=GroupSnapshotList)
def list_group_snapshots(group_id: int, db: Session = Depends(get_db)):
    """Group snapshots (named like the router's snapshots), the VMs holding each one, and whether a revert
    is possible (`problem` in plain words if not)"""
    return snapshot_service.list_group(_group(db, group_id))


@group_router.post("/{group_id}/snapshots", response_model=Task, status_code=202)
def create_group_snapshot(group_id: int, data: SnapshotCreate, db: Session = Depends(get_db)):
    """Pause every VM of the group (router, members, cluster nodes), snapshot each one, resume them"""
    return _run(snapshot_service.create_group, db, _group(db, group_id), data)


@group_router.post("/{group_id}/snapshots/{name}/revert", response_model=Task, status_code=202)
def revert_group_snapshot(group_id: int, name: str = SnapName, db: Session = Depends(get_db)):
    """Revert every VM of the group to the snapshot, resume them together and set their clocks to the
    host's. The group settings come back as they were. Refused if machines were added / removed since."""
    return _run(snapshot_service.revert_group, db, _group(db, group_id), name)


@group_router.delete("/{group_id}/snapshots/{name}", response_model=Task, status_code=202)
def delete_group_snapshot(group_id: int, name: str = SnapName, db: Session = Depends(get_db)):
    """Delete the snapshot on every VM of the group holding it"""
    return _run(snapshot_service.delete_group, db, _group(db, group_id), name)
