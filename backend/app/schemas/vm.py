"""VM schemas"""
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class VMBase(BaseModel):
    """Base VM schema"""
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    description: Optional[str] = None
    memory: int = Field(2048, ge=128)  # MiB
    vcpu: int = Field(2, ge=1, le=512)
    os_type: Optional[str] = None
    arch: str = "x86_64"


class VMCreate(VMBase):
    """VM creation schema"""
    template_id: Optional[int] = None

    # New qcow2 disk created in the default storage pool (GiB). 0/None = no disk.
    disk_size: Optional[int] = Field(20, ge=0)
    # Path of an ISO volume to attach as CD-ROM (see GET /storage/isos)
    iso_path: Optional[str] = None
    # Ready cloud image to copy as the boot disk (see GET /storage/cloud-images).
    # The disk is grown to disk_size if that is larger than the image.
    cloud_image_id: Optional[int] = None

    # cloud-init (only used with cloud_image_id)
    cloudinit_username: Optional[str] = Field(None, pattern=r"^[a-z_][a-z0-9_-]*$")
    cloudinit_password: Optional[str] = None
    cloudinit_ssh_keys: List[str] = []
    cloudinit_userdata: Optional[str] = None  # raw #cloud-config, overrides the fields above
    # Guest keyboard layout (XKB name: us, fr, de, gb, ...). The VNC console sends
    # physical key positions, so this must match the keyboard of whoever types in it.
    cloudinit_keyboard: Optional[str] = Field(None, pattern=r"^[a-z]{2,10}$")

    # Network configuration (None = settings.DEFAULT_NETWORK)
    network_name: Optional[str] = None
    # Fixed MAC (e.g. to match a static DHCP reservation); libvirt picks one if unset
    mac_address: Optional[str] = Field(None, pattern=r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")

    autostart: bool = False
    start: bool = False


class VMUpdate(BaseModel):
    """VM update schema (metadata only)"""
    description: Optional[str] = None
    os_type: Optional[str] = None
    autostart: Optional[bool] = None


class VM(VMBase):
    """VM response schema"""
    model_config = ConfigDict(from_attributes=True)

    name: str
    id: int
    uuid: Optional[str] = None
    status: str = "defined"
    template_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class VMDisk(BaseModel):
    device: Optional[str] = None
    path: Optional[str] = None
    target: Optional[str] = None


class VMInterface(BaseModel):
    name: str
    mac: Optional[str] = None
    addresses: List[str] = []


class VMNic(BaseModel):
    network: Optional[str] = None
    mac: Optional[str] = None


class VMConsole(BaseModel):
    """VM console information"""
    type: str  # vnc, spice
    host: str
    port: Optional[int] = None


class VMDetail(VM):
    """VM with live details from libvirt"""
    autostart: bool = False
    disks: List[VMDisk] = []
    interfaces: List[VMInterface] = []
    nics: List[VMNic] = []
    console: Optional[VMConsole] = None
    xml_config: Optional[str] = None


# Template schemas

class VMTemplateBase(BaseModel):
    """Base VM template schema"""
    name: str
    description: Optional[str] = None
    memory: int = 2048
    vcpu: int = 2


class VMTemplateCreate(VMTemplateBase):
    """VM template creation schema"""
    xml_config: Optional[str] = None
    cloudinit_defaults: Optional[Dict[str, Any]] = None


class VMTemplate(VMTemplateBase):
    """VM template response schema"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    xml_config: Optional[str] = None
    cloudinit_defaults: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime
