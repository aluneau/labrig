"""Host/Node schemas"""
from pydantic import BaseModel
from typing import Optional, List


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
