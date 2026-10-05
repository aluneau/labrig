"""VM device endpoints: CD-ROM media, boot order, disks, NICs, virtual IOMMU

Mounted under /vms before the vms router, so /vms/{id}/disks isn't taken for a power action.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import VM
from app.schemas import (
    DeviceChange, VMBootUpdate, VMCdromUpdate, VMDiskCreate, VMDiskResize, VMIommuUpdate, VMNicCreate, VMNicUpdate,
)
from app.services.vm_service import vm_service

router = APIRouter()


def _vm(vm_id: int, db: Session) -> VM:
    vm = vm_service.get_vm(db, vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    return vm


def _run(func, *args):
    try:
        return func(*args)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{vm_id}/cdrom", response_model=DeviceChange)
def set_cdrom(vm_id: int, data: VMCdromUpdate, db: Session = Depends(get_db)):
    """Insert an ISO (iso_path from GET /storage/isos) or eject (null). Live when the VM runs.
    A VM without a CD-ROM drive gets a SATA one, which appears at its next start."""
    return _run(vm_service.set_cdrom, db, _vm(vm_id, db), data.iso_path)


@router.put("/{vm_id}/boot", response_model=DeviceChange)
def set_boot(vm_id: int, data: VMBootUpdate, db: Session = Depends(get_db)):
    """Persistent boot order ({order}), or a one-shot order for the next start through
    this app ({once: true, order?} - default cdrom then hd); {once: false} cancels it."""
    return _run(vm_service.set_boot, db, _vm(vm_id, db), data.order, data.once)


@router.post("/{vm_id}/disks", response_model=DeviceChange, status_code=201)
def add_disk(vm_id: int, data: VMDiskCreate, db: Session = Depends(get_db)):
    """Create a volume <vm>-disk<N> and attach it (hot-plugged when running)"""
    return _run(vm_service.add_disk, db, _vm(vm_id, db), data.size_gb, data.pool, data.format, data.bus)


@router.put("/{vm_id}/disks/{target}", response_model=DeviceChange)
def resize_disk(vm_id: int, target: str, data: VMDiskResize, db: Session = Depends(get_db)):
    """Grow a disk (live when running). Shrinking is refused."""
    return _run(vm_service.resize_disk, db, _vm(vm_id, db), target, data.size_gb)


@router.delete("/{vm_id}/disks/{target}", response_model=DeviceChange)
def detach_disk(
    vm_id: int,
    target: str,
    delete_volume: bool = Query(False, description="Also delete the disk's volume"),
    db: Session = Depends(get_db),
):
    """Detach a disk (hot-unplug when running; pending=true if the guest hasn't released it,
    then it goes away at the next shutdown). The boot disk can't be detached."""
    return _run(vm_service.detach_disk, db, _vm(vm_id, db), target, delete_volume)


@router.post("/{vm_id}/nics", response_model=DeviceChange, status_code=201)
def add_nic(vm_id: int, data: VMNicCreate, db: Session = Depends(get_db)):
    """Add a NIC on a libvirt network (hot-plugged when running; target = its MAC). Models: virtio,
    e1000e, igb (Intel 82576, emulated SR-IOV: up to 7 VFs in the guest), e1000, rtl8139. On an SR-IOV
    VF pool network (forward mode hostdev) the VM gets a VF from the host instead (needs a host IOMMU)."""
    return _run(vm_service.add_nic, db, _vm(vm_id, db), data.network, data.model, data.mac, data.link_state)


@router.put("/{vm_id}/nics/{mac}", response_model=DeviceChange)
def update_nic(vm_id: int, mac: str, data: VMNicUpdate, db: Session = Depends(get_db)):
    """Set the link state (up / down = cable unplugged) and/or move the NIC to another network,
    live when running and in the saved config"""
    return _run(vm_service.update_nic, db, _vm(vm_id, db), mac, data.link_state, data.network)


@router.delete("/{vm_id}/nics/{mac}", response_model=DeviceChange)
def remove_nic(vm_id: int, mac: str, db: Session = Depends(get_db)):
    """Remove a NIC (hot-unplug when running; pending=true if the guest hasn't released it,
    then it goes away at the next shutdown)"""
    return _run(vm_service.remove_nic, db, _vm(vm_id, db), mac)


@router.put("/{vm_id}/iommu", response_model=DeviceChange)
def set_iommu(vm_id: int, data: VMIommuUpdate, db: Session = Depends(get_db)):
    """Enable / disable the virtual IOMMU (intel-iommu, interrupt remapping, caching mode) in the saved
    config. It applies at the next cold start (power off + start), not on a guest reboot."""
    return _run(vm_service.set_iommu, db, _vm(vm_id, db), data.enabled)
