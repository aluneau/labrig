"""Mirror registry on the group router (contract stub, implementation follows)"""
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Group, Task
from app.schemas.registry import MirrorRequest, MirrorResult, RegistryStatus


def status(db: Session, group: Group) -> RegistryStatus:
    raise NotImplementedError


def ensure_mirrored(db: Session, group: Group, request: MirrorRequest, task: Optional[Task] = None) -> MirrorResult:
    """Blocking, idempotent. Reports progress through `task` and stops when it is cancelled."""
    raise NotImplementedError
