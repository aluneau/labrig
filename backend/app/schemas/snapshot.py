"""Snapshot schemas (VM and lab group snapshots, docs/snapshots.md)"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

# libvirt accepts more, but these names end up in URLs, task names and qcow2 headers
SNAPSHOT_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$"


class SnapshotCreate(BaseModel):
    name: str = Field(..., pattern=SNAPSHOT_NAME, description="Letters, digits, '.', '_' and '-' (max 63)")
    description: Optional[str] = Field(None, max_length=1000)


class VMSnapshot(BaseModel):
    name: str
    description: Optional[str] = None
    created: Optional[datetime] = None
    state: Optional[str] = None  # the VM's state when taken: running, paused (group snapshots), shutoff
    memory: bool = False  # memory state included: a revert resumes the VM where it was
    parent: Optional[str] = None
    current: bool = False  # the snapshot the VM was last taken / reverted to


class VMSnapshotList(BaseModel):
    snapshots: List[VMSnapshot]
    supported: bool
    reasons: List[str] = []  # why snapshots are refused for this VM, in plain words


class GroupSnapshotVM(BaseModel):
    vm: str
    state: Optional[str] = None
    memory: bool = False


class GroupSnapshot(BaseModel):
    name: str
    description: Optional[str] = None
    created: Optional[datetime] = None
    vms: List[GroupSnapshotVM]
    revertable: bool
    problem: Optional[str] = None  # why a revert is refused (members added / removed since...)


class GroupSnapshotList(BaseModel):
    snapshots: List[GroupSnapshot]
    vms: List[str]  # the group's VMs now: router, members, cluster nodes
    supported: bool
    reasons: List[str] = []
