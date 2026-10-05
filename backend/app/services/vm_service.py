"""VM Service

libvirt is the source of truth for VM state; the database only stores
metadata (description, os_type, ...) keyed by the libvirt domain UUID.
"""
import io
import logging
from typing import List, Optional, Dict, Any
from xml.sax.saxutils import quoteattr

from sqlalchemy.orm import Session

from app import domain_xml
from app.config import settings
from app.database import serialized
from app.libvirt_client import libvirt_client
from app.models import VM, Volume
from app.schemas import VMCreate, VMUpdate
from app.services.cloud_image_service import cloud_image_service

logger = logging.getLogger(__name__)


class VMService:
    """VM lifecycle management service"""

    @serialized
    def sync_vms(self, db: Session) -> None:
        """Mirror libvirt domains into the database"""
        libvirt_vms = {vm["uuid"]: vm for vm in libvirt_client.list_vms()}

        for vm in db.query(VM).all():
            if vm.uuid not in libvirt_vms:
                db.query(Volume).filter(Volume.vm_id == vm.id).update({Volume.vm_id: None})
                db.delete(vm)

        for lv_vm in libvirt_vms.values():
            vm = db.query(VM).filter(VM.uuid == lv_vm["uuid"]).first()
            if vm is None:
                vm = VM(uuid=lv_vm["uuid"])
                db.add(vm)
            vm.name = lv_vm["name"]
            vm.status = lv_vm["state"]
            vm.memory = lv_vm["memory"] // 1024  # KiB -> MiB
            vm.vcpu = lv_vm["vcpu"]

        db.commit()

    def list_vms(self, db: Session) -> List[VM]:
        self.sync_vms(db)
        return db.query(VM).order_by(VM.name).all()

    def get_vm(self, db: Session, vm_id: int) -> Optional[VM]:
        return db.query(VM).filter(VM.id == vm_id).first()

    def get_vm_detail(self, db: Session, vm_id: int) -> Optional[Dict[str, Any]]:
        vm = self.get_vm(db, vm_id)
        if not vm:
            return None
        live = libvirt_client.get_vm(vm.name)
        if live is None:
            return None
        vm.status = live["state"]
        db.commit()
        devices = libvirt_client.get_vm_devices(vm.name) or {"disks": [], "cdrom": None, "boot_order": []}
        return {
            **{c.name: getattr(vm, c.name) for c in VM.__table__.columns},
            "autostart": live["autostart"],
            "xml_config": live["xml"],
            "disks": devices["disks"],
            "cdrom": devices["cdrom"],
            "boot": {"order": devices["boot_order"], "once": vm.next_boot.split(",") if vm.next_boot else None},
            "interfaces": libvirt_client.get_vm_interfaces(vm.name),
            "nics": libvirt_client.get_vm_nics(vm.name),
            "console": libvirt_client.get_vm_console(vm.name),
        }

    def create_vm(self, db: Session, vm_data: VMCreate) -> VM:
        if libvirt_client.get_vm(vm_data.name) is not None:
            raise ValueError(f"VM with name '{vm_data.name}' already exists")

        image = None
        if vm_data.cloud_image_id:
            image = cloud_image_service.get_image(db, vm_data.cloud_image_id)
            if not image or image.status != "ready":
                raise ValueError("Cloud image not found or not downloaded yet")
            if vm_data.iso_path:
                raise ValueError("Choose either a cloud image or an ISO, not both")

        pool_name = libvirt_client.ensure_pool(settings.DEFAULT_POOL_NAME, settings.DEFAULT_POOL_PATH).name()
        disk_size = (vm_data.disk_size or 0) * 1024 ** 3
        created: List[str] = []  # volume paths to clean up if anything fails

        try:
            disk_path = None
            seed_path = None
            if image:
                disk_path = libvirt_client.clone_volume(pool_name, image.path, f"{vm_data.name}.qcow2", disk_size)
                created.append(disk_path)
                user_data = cloud_image_service.build_user_data(
                    hostname=vm_data.name,
                    username=vm_data.cloudinit_username,
                    password=vm_data.cloudinit_password,
                    ssh_keys=[k.strip() for k in vm_data.cloudinit_ssh_keys if k.strip()],
                    custom=vm_data.cloudinit_userdata,
                    keyboard=vm_data.cloudinit_keyboard,
                )
                seed = cloud_image_service.build_seed_iso(vm_data.name, user_data)
                seed_path = libvirt_client.upload_volume(
                    pool_name, f"{vm_data.name}-cidata.iso", len(seed), io.BytesIO(seed))
                created.append(seed_path)
            elif disk_size:
                disk_path = libvirt_client.create_volume(pool_name, f"{vm_data.name}.qcow2", disk_size, "qcow2")
                created.append(disk_path)

            vm_uuid = libvirt_client.create_vm(
                name=vm_data.name,
                memory=vm_data.memory,
                vcpu=vm_data.vcpu,
                disk_xml=self._build_disk_xml(disk_path, vm_data.iso_path, seed_path),
                network_xml=self._build_network_xml(vm_data.network_name or settings.DEFAULT_NETWORK,
                                                    vm_data.mac_address),
                arch=vm_data.arch,
                boot_devs=["hd", "cdrom"] if vm_data.iso_path else ["hd"],
            )
        except Exception:
            for path in created:
                libvirt_client.delete_volume_by_path(path)
            raise

        if vm_data.autostart:
            libvirt_client.set_autostart(vm_data.name, True)

        db_vm = VM(
            uuid=vm_uuid,
            name=vm_data.name,
            description=vm_data.description,
            memory=vm_data.memory,
            vcpu=vm_data.vcpu,
            os_type=vm_data.os_type or (f"{image.distribution} {image.version}" if image else None),
            arch=vm_data.arch,
            status="shutoff",
            template_id=vm_data.template_id,
        )
        db.add(db_vm)
        db.commit()
        db.refresh(db_vm)

        if vm_data.start:
            libvirt_client.start_vm(vm_data.name)
            db_vm.status = "running"
            db.commit()

        return db_vm

    def update_vm(self, db: Session, vm_id: int, vm_data: VMUpdate) -> Optional[VM]:
        vm = self.get_vm(db, vm_id)
        if not vm:
            return None

        update_data = vm_data.model_dump(exclude_unset=True)
        autostart = update_data.pop("autostart", None)
        if autostart is not None:
            libvirt_client.set_autostart(vm.name, autostart)
        for field, value in update_data.items():
            setattr(vm, field, value)

        db.commit()
        db.refresh(vm)
        return vm

    def delete_vm(self, db: Session, vm_id: int, delete_disks: bool = False) -> bool:
        vm = self.get_vm(db, vm_id)
        if not vm:
            return False
        libvirt_client.delete_vm(vm.name, delete_disks=delete_disks)
        db.query(Volume).filter(Volume.vm_id == vm.id).update({Volume.vm_id: None})
        db.delete(vm)
        db.commit()
        return True

    def power_action(self, db: Session, vm_id: int, action: str) -> Optional[VM]:
        """Run start/stop/force_stop/reboot/suspend/resume and refresh state"""
        vm = self.get_vm(db, vm_id)
        if not vm:
            return None

        actions = {
            "start": lambda: self._start(db, vm),
            "stop": lambda: libvirt_client.stop_vm(vm.name),
            "force_stop": lambda: libvirt_client.stop_vm(vm.name, force=True),
            "reboot": lambda: libvirt_client.reboot_vm(vm.name),
            "suspend": lambda: libvirt_client.suspend_vm(vm.name),
            "resume": lambda: libvirt_client.resume_vm(vm.name),
        }
        if action not in actions:
            raise ValueError(f"Unknown action '{action}'")
        actions[action]()

        live = libvirt_client.get_vm(vm.name)
        if live:
            vm.status = live["state"]
            db.commit()
        return vm

    def _start(self, db: Session, vm: VM) -> None:
        """Start, applying a pending one-shot boot order (PUT /vms/{id}/boot once=true)"""
        if not vm.next_boot:
            libvirt_client.start_vm(vm.name)
            return
        libvirt_client.start_vm_with_boot_order(vm.name, vm.next_boot.split(","))
        vm.next_boot = None
        db.commit()

    # Devices: CD-ROM, boot order, disks

    def set_cdrom(self, db: Session, vm: VM, iso_path: Optional[str]) -> Dict[str, Any]:
        if iso_path and not libvirt_client.volume_exists(iso_path):
            raise ValueError(f"{iso_path} is not a volume of an active storage pool (see GET /storage/isos)")
        result = libvirt_client.set_cdrom(vm.name, iso_path)
        if result["target"] is None:
            return {"message": "The VM has no CD-ROM drive", "pending": False}
        message = f"Inserted {iso_path.rsplit('/', 1)[-1]}" if iso_path else "Ejected the CD-ROM"
        if result["pending"]:
            message += ("; the VM had no CD-ROM drive: one was added to its configuration and appears "
                        "at the next start (SATA drives can't be hot-plugged)")
        return {"message": message, "pending": result["pending"], "target": result["target"], "path": iso_path}

    def set_boot(self, db: Session, vm: VM, order: Optional[List[str]], once: Optional[bool]) -> Dict[str, Any]:
        if once:
            order = order or ["cdrom", "hd"]
            devices = libvirt_client.get_vm_devices(vm.name)
            if "cdrom" in order and devices is not None and devices["cdrom"] is None:
                raise ValueError("The VM has no CD-ROM drive: insert an ISO first (PUT /vms/{id}/cdrom)")
            vm.next_boot = ",".join(order)
            db.commit()
            libvirt_client.publish_vm_event(vm.name, "devices")
            return {"message": f"The next start through this app boots from {order[0]} (once)", "pending": True}
        messages = []
        if once is False and vm.next_boot:
            vm.next_boot = None
            db.commit()
            libvirt_client.publish_vm_event(vm.name, "devices")
            messages.append("One-shot boot cancelled")
        if order:
            libvirt_client.set_boot_order(vm.name, order)
            running = vm.status in ("running", "paused", "blocked")
            messages.append("Boot order saved" + ("; it applies at the next start" if running else ""))
        return {"message": ". ".join(messages) or "Nothing to change", "pending": False}

    def add_disk(self, db: Session, vm: VM, size_gb: int, pool: Optional[str], fmt: str, bus: str) -> Dict[str, Any]:
        pool_name = pool or libvirt_client.ensure_pool(settings.DEFAULT_POOL_NAME, settings.DEFAULT_POOL_PATH).name()
        name = libvirt_client.free_volume_name(pool_name, f"{vm.name}-disk", "qcow2" if fmt == "qcow2" else "img")
        target = libvirt_client.next_disk_target(vm.name, "vd" if bus == "virtio" else "sd")
        path = libvirt_client.create_volume(pool_name, name, size_gb * 1024 ** 3, fmt)
        try:
            result = libvirt_client.attach_disk(vm.name, domain_xml.data_disk_xml(path, target, bus, fmt))
        except Exception:
            libvirt_client.delete_volume_by_path(path)
            raise
        message = f"Added {size_gb} GiB disk {target} ({name})"
        if result["pending"]:
            message += "; it appears at the next start"
            if result["error"]:
                message += f" (hot-plug failed: {result['error']})"
        return {"message": message, "pending": result["pending"], "target": target, "path": path}

    def detach_disk(self, db: Session, vm: VM, target: str, delete_volume: bool) -> Dict[str, Any]:
        disk = self._find_disk(vm, target)
        if disk["boot"]:
            raise ValueError(f"{target} is the boot disk; it can't be detached")
        result = libvirt_client.detach_disk(vm.name, target)
        path = result["path"]
        if result["pending"]:
            message = (f"{target} is removed from the configuration, but the guest has not released it yet: "
                       "it goes away at the next shutdown")
            if delete_volume:
                message += f". The volume {path} is kept while in use; delete it from Storage later"
            return {"message": message, "pending": True, "target": target, "path": path}
        message = f"Detached {target}"
        if delete_volume and path:
            message += f" and deleted {path}" if libvirt_client.delete_volume_by_path(path) \
                else f" (could not delete {path})"
        return {"message": message, "pending": False, "target": target, "path": path}

    def resize_disk(self, db: Session, vm: VM, target: str, size_gb: int) -> Dict[str, Any]:
        disk = self._find_disk(vm, target)
        new = size_gb * 1024 ** 3
        current = disk["capacity"] or 0
        if new == current:
            return {"message": f"{target} is already {size_gb} GiB", "pending": False, "target": target,
                    "path": disk["path"]}
        if new < current:
            raise ValueError(f"{target} is {current / 1024 ** 3:.1f} GiB: disks can only grow")
        libvirt_client.resize_disk(vm.name, target, new)
        message = f"Resized {target} to {size_gb} GiB"
        if disk["pending"] is None and vm.status in ("running", "paused"):
            message += " (the guest sees the new size now; grow its partition / filesystem inside the guest)"
        return {"message": message, "pending": False, "target": target, "path": disk["path"]}

    def _find_disk(self, vm: VM, target: str) -> Dict[str, Any]:
        devices = libvirt_client.get_vm_devices(vm.name)
        disk = next((d for d in (devices or {}).get("disks", []) if d["target"] == target), None)
        if disk is None or disk["device"] != "disk":
            raise LookupError(f"VM {vm.name} has no disk {target}")
        return disk

    def get_console(self, db: Session, vm_id: int) -> Optional[Dict[str, Any]]:
        vm = self.get_vm(db, vm_id)
        if not vm:
            return None
        return libvirt_client.get_vm_console(vm.name)

    def _build_disk_xml(self, disk_path: Optional[str], iso_path: Optional[str],
                        seed_path: Optional[str] = None) -> str:
        """Boot disk vda, a CD-ROM sda (empty unless iso_path, so media can be swapped live later)
        and the cloud-init seed on its own CD-ROM sdb"""
        disks = []
        if disk_path:
            disks.append(domain_xml.data_disk_xml(disk_path, "vda", "virtio", "qcow2"))
        disks.append(domain_xml.cdrom_xml("sda", iso_path))
        if seed_path:
            disks.append(domain_xml.cdrom_xml("sdb", seed_path))
        return "\n".join(disks)

    def _build_network_xml(self, network_name: str, mac: Optional[str] = None) -> str:
        mac_xml = f"<mac address={quoteattr(mac.lower())}/>" if mac else ""
        return f"""
        <interface type='network'>
            {mac_xml}
            <source network={quoteattr(network_name)}/>
            <model type='virtio'/>
        </interface>
        """


vm_service = VMService()
