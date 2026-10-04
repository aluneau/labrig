"""Task Service: background jobs tracked in the database"""
import uuid
import logging
from datetime import datetime, timezone
from threading import Thread
from typing import List, Optional, Dict, Any, Callable

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.events import event_bus
from app.models import Task
from app.schemas import TaskCreate, TaskUpdate

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TaskService:
    """Background task management service"""

    def __init__(self):
        self._running: Dict[int, Thread] = {}
        self._cancelled: set[int] = set()

    def list_tasks(self, db: Session, status: Optional[str] = None, limit: int = 100) -> List[Task]:
        query = db.query(Task)
        if status:
            query = query.filter(Task.status == status)
        return query.order_by(Task.created_at.desc()).limit(limit).all()

    def get_task(self, db: Session, task_id: int) -> Optional[Task]:
        return db.query(Task).filter(Task.id == task_id).first()

    def create_task(self, db: Session, task_data: TaskCreate) -> Task:
        db_task = Task(
            uuid=str(uuid.uuid4()),
            name=task_data.name,
            description=task_data.description,
            type=task_data.type,
            status="pending",
            progress=0,
            target_type=task_data.target_type,
            target_id=task_data.target_id,
            target_name=task_data.target_name,
        )
        db.add(db_task)
        db.commit()
        db.refresh(db_task)
        return db_task

    def update_task(self, db: Session, task_id: int, task_data: TaskUpdate) -> Optional[Task]:
        task = self.get_task(db, task_id)
        if not task:
            return None

        for field, value in task_data.model_dump(exclude_unset=True).items():
            setattr(task, field, value)

        if task_data.status == "running" and not task.started_at:
            task.started_at = _now()
        elif task_data.status in ("completed", "failed", "cancelled"):
            task.completed_at = _now()

        db.commit()
        db.refresh(task)
        self._publish(task)
        return task

    def _publish(self, task: Task) -> None:
        event_bus.publish({"kind": "task", "id": task.id, "status": task.status, "progress": task.progress,
                           "target_type": task.target_type, "target_name": task.target_name})

    def delete_task(self, db: Session, task_id: int) -> bool:
        task = self.get_task(db, task_id)
        if not task:
            return False
        if task_id in self._running:
            raise ValueError("Cannot delete a running task, cancel it first")
        db.delete(task)
        db.commit()
        return True

    def start(self, db: Session, task_data: TaskCreate, func: Callable, *args, **kwargs) -> Task:
        """Create a task and run func(db, task, *args, **kwargs) in a background thread.

        func's return value (a dict) is stored as the task result; an exception
        marks the task failed with its message.
        """
        task = self.create_task(db, task_data)
        task_id = task.id
        self.update_task(db, task_id, TaskUpdate(status="running"))

        def run():
            session = SessionLocal()
            try:
                result = func(session, self.get_task(session, task_id), *args, **kwargs)
                self.update_task(session, task_id, TaskUpdate(status="completed", progress=100, result=result))
            except Exception as e:
                cancelled = task_id in self._cancelled
                if not cancelled:
                    logger.exception(f"Task {task_id} failed")
                self.update_task(
                    session, task_id,
                    TaskUpdate(status="cancelled") if cancelled
                    else TaskUpdate(status="failed", error_message=str(e)),
                )
            finally:
                session.close()
                self._running.pop(task_id, None)
                self._cancelled.discard(task_id)

        thread = Thread(target=run, daemon=True, name=f"task-{task_id}")
        self._running[task_id] = thread
        thread.start()
        db.refresh(task)
        return task

    def update_progress(self, db: Session, task_id: int, progress: int) -> None:
        task = self.get_task(db, task_id)
        if task:
            task.progress = min(max(progress, 0), 100)
            db.commit()
            self._publish(task)

    def is_cancelled(self, task_id: int) -> bool:
        return task_id in self._cancelled

    def cancel_task(self, db: Session, task_id: int) -> bool:
        """Ask a running task to stop; the task body must poll is_cancelled()"""
        if task_id not in self._running:
            return False
        self._cancelled.add(task_id)
        return True

    def mark_interrupted(self, db: Session) -> None:
        """Tasks still 'running' from a previous process can never finish"""
        for task in db.query(Task).filter(Task.status.in_(["pending", "running"])).all():
            task.status = "failed"
            task.error_message = "Interrupted by server restart"
            task.completed_at = _now()
        db.commit()

    def get_task_stats(self, db: Session) -> Dict[str, Any]:
        stats = {"total": db.query(Task).count()}
        for status in ("pending", "running", "completed", "failed", "cancelled"):
            stats[status] = db.query(Task).filter(Task.status == status).count()
        return stats


task_service = TaskService()
