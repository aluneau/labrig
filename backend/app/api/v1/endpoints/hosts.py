"""Host endpoints"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import HostInfo, HostResources
from app.services.host_service import host_service

router = APIRouter()


@router.get("/info", response_model=HostInfo)
def get_host_info(db: Session = Depends(get_db)):
    """Get host information"""
    info = host_service.get_host_info(db)
    return info


@router.get("/resources", response_model=HostResources)
def get_host_resources():
    """Get current resource usage"""
    resources = host_service.get_resources()
    return resources


@router.get("/capabilities")
def get_host_capabilities():
    """Get host capabilities"""
    caps = host_service.get_capabilities()
    return caps


@router.get("/logs")
def get_host_logs(lines: int = Query(100, ge=1, le=1000)):
    """Get system logs"""
    logs = host_service.get_system_logs(lines)
    return {"logs": logs}
