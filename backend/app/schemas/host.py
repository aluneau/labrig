"""Host/Node schemas"""
from pydantic import BaseModel, Field
from typing import Optional, List

from app.schemas.task import Task


class HostResources(BaseModel):
    """Host resource usage"""
    cpu_count: int
    cpu_usage_percent: float
    memory_total: int  # bytes
    memory_used: int  # bytes
    memory_free: int  # bytes
    memory_usage_percent: float
    disk_total: int  # bytes (filesystem of the default pool)
    disk_used: int  # bytes
    disk_free: int  # bytes
    disk_usage_percent: float


class HostInfo(BaseModel):
    """Host information"""
    hostname: str
    arch: str
    cpu_model: Optional[str] = None
    cpus: int
    sockets: int
    cores: int
    threads: int
    mhz: int
    memory: int  # bytes
    os_type: str
    os_version: str
    kernel_version: str
    libvirt_uri: str
    libvirt_version: str
    qemu_version: Optional[str] = None
    kvm_available: bool
    emulator_available: bool
    issues: List[str] = []

    resources: HostResources

    total_vms: int
    running_vms: int
    stopped_vms: int
    paused_vms: int

    total_pools: int
    total_volumes: int

    total_networks: int
    active_networks: int


class LibvirtUnit(BaseModel):
    name: str
    active_state: str  # active | inactive | activating | deactivating | failed
    enabled: Optional[str] = None  # enabled | disabled | static | ...


class LibvirtStatus(BaseModel):
    """State of the libvirt daemon(s), read from systemd without connecting (no socket activation)"""
    state: str  # running | stopped | starting | stopping
    connected: bool  # the app holds an open connection right now
    manageable: bool  # Start/Stop available (local qemu:///system with systemd)
    mode: Optional[str] = None  # monolithic (libvirtd) | modular (virtqemud, virtnetworkd, ...)
    daemon_active: bool = False  # the daemon process runs (False when only its sockets listen)
    units: List[LibvirtUnit] = []
    idle_timeout_minutes: float
    helper_installed: bool
    dhcp_release_available: bool
    uri: str


class LibvirtStop(BaseModel):
    # refuse: fail with 409 if VMs run; shutdown: ACPI-shut them down first (a task); force: stop anyway
    mode: str = Field("refuse", pattern=r"^(refuse|shutdown|force)$")
    timeout: int = Field(120, ge=10, le=1800)  # seconds to wait for VMs in "shutdown" mode


class LibvirtAction(BaseModel):
    """Result of a start / stop request"""
    status: LibvirtStatus
    task: Optional[Task] = None  # "shutdown" mode: background task shutting VMs down, then stopping libvirt
    warning: Optional[str] = None
