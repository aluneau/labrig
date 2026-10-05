"""Host endpoints"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import HostInfo, HostResources, LibvirtStatus, LibvirtStop, LibvirtAction, TaskCreate
from app.services.daemon_service import daemon_service
from app.services.host_service import host_service
from app.services.task_service import task_service

router = APIRouter()


@router.get("/libvirt", response_model=LibvirtStatus)
def get_libvirt_status():
    """libvirt daemon state (running | stopped | starting | stopping), read from systemd: never starts it"""
    return daemon_service.status()


@router.post("/libvirt/start", response_model=LibvirtAction)
def start_libvirt():
    """Start libvirt (libvirtd, or virtqemud & co on modular hosts) and connect"""
    return {"status": daemon_service.start()}


@router.post("/libvirt/stop", response_model=LibvirtAction)
def stop_libvirt(data: LibvirtStop, db: Session = Depends(get_db)):
    """Stop libvirt and its sockets. With running VMs: mode=refuse -> 409, mode=shutdown -> a task that
    shuts them down first, mode=force -> stop anyway (QEMU processes keep running unmanaged)."""
    running = daemon_service.running_vms() if daemon_service.manageable() else []
    if running and data.mode == "refuse":
        raise HTTPException(status_code=409, detail=f"{len(running)} VM(s) running: {', '.join(running)}. "
                                                    f"Shut them down first, or stop libvirt anyway.")
    if running and data.mode == "shutdown":
        task = task_service.start(
            db, TaskCreate(name="Shut down VMs and stop libvirt", type="libvirt_stop",
                           description=f"Shutting down {', '.join(running)}",
                           target_type="host", target_name="libvirt"),
            daemon_service.shutdown_vms_and_stop, data.timeout,
        )
        return {"status": daemon_service.status(), "task": task}
    warning = None
    if running:
        warning = (f"{len(running)} VM(s) keep running without libvirt ({', '.join(running)}): their QEMU "
                   f"processes stay up but can't be managed until libvirt starts again and picks them up.")
    return {"status": daemon_service.stop(), "warning": warning}


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
