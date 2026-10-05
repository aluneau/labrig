"""VM Service

libvirt is the source of truth for VM state; the database only stores
metadata (description, os_type, ...) keyed by the libvirt domain UUID.
"""
import io
import logging
from typing import List, Optional, Dict, Any, Tuple
from xml.sax.saxutils import quoteattr

from sqlalchemy.orm import Session

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
        return {
            **{c.name: getattr(vm, c.name) for c in VM.__table__.columns},
            "autostart": live["autostart"],
            "xml_config": live["xml"],
            "disks": libvirt_client.get_vm_disks(vm.name),
            "interfaces": libvirt_client.get_vm_interfaces(vm.name),
            "nics": libvirt_client.get_vm_nics(vm.name),
            "console": libvirt_client.get_vm_console(vm.name),
        }

    def create_vm(self, db: Session, vm_data: VMCreate, *, fqdn: Optional[str] = None,
                  user_data: Optional[str] = None, network_config: Optional[str] = None,
                  nics: Optional[List[Tuple[str, Optional[str]]]] = None, metadata_xml: str = "",
                  hostname: Optional[str] = None) -> VM:
        """Create a VM. The keyword-only options are for internal callers (lab groups):
        guest hostname/fqdn, a prebuilt user-data / network-config, several NICs
        [(network, mac)] instead of vm_data.network_name, and <metadata> content."""
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
                if user_data is None:
                    user_data = cloud_image_service.build_user_data(
                        hostname=hostname or vm_data.name,
                        username=vm_data.cloudinit_username,
                        password=vm_data.cloudinit_password,
                        ssh_keys=[k.strip() for k in vm_data.cloudinit_ssh_keys if k.strip()],
                        custom=vm_data.cloudinit_userdata,
                        keyboard=vm_data.cloudinit_keyboard,
                        fqdn=fqdn,
                    )
                seed = cloud_image_service.build_seed_iso(hostname or vm_data.name, user_data, network_config)
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
                disk_xml=self._build_disk_xml(disk_path, vm_data.iso_path or seed_path),
                network_xml="\n".join(
                    self._build_network_xml(network, mac) for network, mac in
                    (nics or [(vm_data.network_name or settings.DEFAULT_NETWORK, vm_data.mac_address)])),
                arch=vm_data.arch,
                boot_devs=["hd", "cdrom"] if vm_data.iso_path else ["hd"],
                metadata_xml=metadata_xml,
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
            "start": lambda: libvirt_client.start_vm(vm.name),
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

    def get_console(self, db: Session, vm_id: int) -> Optional[Dict[str, Any]]:
        vm = self.get_vm(db, vm_id)
        if not vm:
            return None
        return libvirt_client.get_vm_console(vm.name)

    def _build_disk_xml(self, disk_path: Optional[str], iso_path: Optional[str]) -> str:
        disks = []
        if disk_path:
            disks.append(f"""
            <disk type='file' device='disk'>
                <driver name='qemu' type='qcow2' discard='unmap'/>
                <source file={quoteattr(disk_path)}/>
                <target dev='vda' bus='virtio'/>
            </disk>
            """)
        if iso_path:
            disks.append(f"""
            <disk type='file' device='cdrom'>
                <driver name='qemu' type='raw'/>
                <source file={quoteattr(iso_path)}/>
                <target dev='sda' bus='sata'/>
                <readonly/>
            </disk>
            """)
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
