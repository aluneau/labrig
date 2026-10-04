"""VM endpoints"""
import asyncio
from typing import List, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.schemas import VM, VMCreate, VMUpdate, VMDetail, VMConsole
from app.services.vm_service import vm_service

router = APIRouter()

PowerAction = Literal["start", "stop", "force_stop", "reboot", "suspend", "resume"]


@router.get("", response_model=List[VM])
def list_vms(db: Session = Depends(get_db)):
    """List all VMs (synced from libvirt)"""
    return vm_service.list_vms(db)


@router.post("/sync")
def sync_vms(db: Session = Depends(get_db)):
    """Sync VMs from libvirt to database"""
    vm_service.sync_vms(db)
    return {"message": "VMs synced successfully"}


@router.get("/{vm_id}", response_model=VMDetail)
def get_vm(vm_id: int, db: Session = Depends(get_db)):
    """Get VM with live details (disks, IPs, console)"""
    vm = vm_service.get_vm_detail(db, vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    return vm


@router.post("", response_model=VM, status_code=201)
def create_vm(vm_data: VMCreate, db: Session = Depends(get_db)):
    """Create a new VM"""
    try:
        return vm_service.create_vm(db, vm_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{vm_id}", response_model=VM)
def update_vm(vm_id: int, vm_data: VMUpdate, db: Session = Depends(get_db)):
    """Update VM metadata / autostart"""
    vm = vm_service.update_vm(db, vm_id, vm_data)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    return vm


@router.delete("/{vm_id}")
def delete_vm(
    vm_id: int,
    delete_disks: bool = Query(False, description="Also delete the VM's disk volumes"),
    db: Session = Depends(get_db),
):
    """Delete VM (powers it off first if running)"""
    if not vm_service.delete_vm(db, vm_id, delete_disks=delete_disks):
        raise HTTPException(status_code=404, detail="VM not found")
    return {"message": "VM deleted successfully"}


@router.post("/{vm_id}/{action}", response_model=VM)
def vm_power_action(vm_id: int, action: PowerAction, db: Session = Depends(get_db)):
    """Power actions: start, stop (ACPI), force_stop, reboot, suspend, resume"""
    vm = vm_service.power_action(db, vm_id, action)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    return vm


@router.get("/{vm_id}/console", response_model=VMConsole)
def get_vm_console(vm_id: int, db: Session = Depends(get_db)):
    """Get VM graphical console information"""
    console = vm_service.get_console(db, vm_id)
    if not console:
        raise HTTPException(status_code=404, detail="Console not available")
    return console


def _console_for(vm_id: int):
    with SessionLocal() as db:
        return vm_service.get_console(db, vm_id)


@router.websocket("/{vm_id}/vnc")
async def vm_vnc(websocket: WebSocket, vm_id: int):
    """WebSocket <-> VNC bridge for noVNC (the VM's VNC server stays on localhost)"""
    console = await run_in_threadpool(_console_for, vm_id)
    subprotocol = "binary" if "binary" in websocket.scope.get("subprotocols", []) else None
    await websocket.accept(subprotocol=subprotocol)

    if not console or console.get("type") != "vnc" or not console.get("port"):
        await websocket.close(code=4404, reason="VM is not running or has no VNC console")
        return

    try:
        reader, writer = await asyncio.open_connection(console["host"], console["port"])
    except OSError as e:
        await websocket.close(code=4502, reason=f"Cannot reach VNC server: {e}")
        return

    async def browser_to_vnc():
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                writer.write(message.get("bytes") or (message.get("text") or "").encode())
                await writer.drain()
        except (WebSocketDisconnect, ConnectionError):
            pass

    async def vnc_to_browser():
        try:
            while data := await reader.read(65536):
                await websocket.send_bytes(data)
        except (WebSocketDisconnect, ConnectionError, RuntimeError):
            pass

    tasks = [asyncio.create_task(browser_to_vnc()), asyncio.create_task(vnc_to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        writer.close()
        try:
            await websocket.close()
        except (WebSocketDisconnect, RuntimeError):
            pass  # already closed by the browser
