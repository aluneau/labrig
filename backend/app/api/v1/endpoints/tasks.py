"""Task endpoints"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import Task
from app.services.task_service import task_service

router = APIRouter()


@router.get("", response_model=List[Task])
def list_tasks(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db)
):
    """List tasks, newest first"""
    return task_service.list_tasks(db, status=status, limit=limit)


@router.get("/stats")
def get_task_stats(db: Session = Depends(get_db)):
    """Get task statistics"""
    return task_service.get_task_stats(db)


@router.get("/{task_id}", response_model=Task)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = task_service.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.delete("/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    try:
        if not task_service.delete_task(db, task_id):
            raise HTTPException(status_code=404, detail="Task not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": "Task deleted successfully"}


@router.post("/{task_id}/cancel")
def cancel_task(task_id: int, db: Session = Depends(get_db)):
    """Request cancellation of a running task"""
    if not task_service.cancel_task(db, task_id):
        raise HTTPException(status_code=400, detail="Task not found or not running")
    return {"message": "Cancellation requested"}
