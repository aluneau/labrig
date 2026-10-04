"""Task models for async operations"""
from sqlalchemy import Column, Integer, String, DateTime, Text, JSON
from datetime import datetime

from app.database import Base


class Task(Base):
    """Async Task model"""
    __tablename__ = "tasks"
    
    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String(36), unique=True, index=True)
    
    # Task info
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    
    # Task type
    type = Column(String(50), nullable=False)  # vm_create, vm_start, vm_stop, etc.
    
    # Status
    status = Column(String(20), default="pending")  # pending, running, completed, failed, cancelled
    
    # Progress
    progress = Column(Integer, default=0)  # percentage
    
    # Result
    result = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    
    # Target (what the task is operating on)
    target_type = Column(String(50), nullable=True)  # vm, volume, network, etc.
    target_id = Column(Integer, nullable=True)
    target_name = Column(String(255), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    # User (for future auth)
    user_id = Column(Integer, nullable=True)
