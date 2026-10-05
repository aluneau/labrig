"""Pydantic schemas"""
from app.schemas.vm import (
    VM, VMCreate, VMUpdate, VMDetail, VMConsole, VMDisk, VMInterface, VMTemplate, VMTemplateCreate,
    VMCdrom, VMBoot, VMBootUpdate, VMCdromUpdate, VMDiskCreate, VMDiskResize, DeviceChange,
    VMNic, VMNicCreate, VMNicUpdate, VMIommu, VMIommuUpdate,
)
from app.schemas.storage import (
    StoragePool, StoragePoolCreate,
    Volume, VolumeCreate,
    ISOImage, ISODownload,
    CloudImage, CloudImageDownload,
)
from app.schemas.network import (
    Network, NetworkCreate, NetworkDetail, DHCPLease, NetworkUpdate, NetworkXML, DHCPHost, NetworkInterface,
    NetworkConfig, LeaseRelease,
)
from app.schemas.task import Task, TaskCreate, TaskUpdate
from app.schemas.host import (
    HostInfo, HostResources, LibvirtStatus, LibvirtUnit, LibvirtStop, LibvirtAction, SriovStatus, SriovPF,
    SriovNumVfs,
)
from app.schemas.cluster import Cluster, ClusterCreate, ClusterScale, ClusterNodeOut, ClusterCommandOutput

__all__ = [
    "VM", "VMCreate", "VMUpdate", "VMDetail", "VMConsole", "VMDisk", "VMInterface",
    "VMTemplate", "VMTemplateCreate",
    "VMCdrom", "VMBoot", "VMBootUpdate", "VMCdromUpdate", "VMDiskCreate", "VMDiskResize", "DeviceChange",
    "VMNic", "VMNicCreate", "VMNicUpdate", "VMIommu", "VMIommuUpdate",
    "StoragePool", "StoragePoolCreate", "Volume", "VolumeCreate",
    "ISOImage", "ISODownload", "CloudImage", "CloudImageDownload",
    "Network", "NetworkCreate", "NetworkDetail", "DHCPLease", "NetworkUpdate", "NetworkXML", "DHCPHost",
    "NetworkInterface", "NetworkConfig", "LeaseRelease",
    "Task", "TaskCreate", "TaskUpdate",
    "HostInfo", "HostResources", "LibvirtStatus", "LibvirtUnit", "LibvirtStop", "LibvirtAction",
    "SriovStatus", "SriovPF", "SriovNumVfs",
    "Cluster", "ClusterCreate", "ClusterScale", "ClusterNodeOut", "ClusterCommandOutput",
]
