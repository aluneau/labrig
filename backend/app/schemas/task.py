"""Task schemas"""
from pydantic import BaseModel, ConfigDict
from typing import Optional, Dict, Any
from datetime import datetime


class TaskBase(BaseModel):
    """Base task schema"""
    name: str
    description: Optional[str] = None
    type: str


class TaskCreate(TaskBase):
    """Task creation schema"""
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    target_name: Optional[str] = None


class TaskUpdate(BaseModel):
    """Task update schema"""
    status: Optional[str] = None
    progress: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


class Task(TaskBase):
    """Task response schema"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    uuid: str
    status: str
    progress: int
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    target_name: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    user_id: Optional[int] = None
