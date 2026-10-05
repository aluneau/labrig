"""Pydantic schemas"""
from app.schemas.vm import (
    VM, VMCreate, VMUpdate, VMDetail, VMConsole, VMDisk, VMInterface, VMTemplate, VMTemplateCreate,
)
from app.schemas.storage import (
    StoragePool, StoragePoolCreate,
    Volume, VolumeCreate,
    ISOImage, ISODownload,
    CloudImage, CloudImageDownload,
)
from app.schemas.network import (
    Network, NetworkCreate, NetworkDetail, DHCPLease, NetworkUpdate, NetworkXML, DHCPHost, NetworkInterface,
    NetworkConfig,
)
from app.schemas.task import Task, TaskCreate, TaskUpdate
from app.schemas.host import HostInfo, HostResources
from app.schemas.cluster import Cluster, ClusterCreate, ClusterScale, ClusterNodeOut, ClusterCommandOutput

__all__ = [
    "VM", "VMCreate", "VMUpdate", "VMDetail", "VMConsole", "VMDisk", "VMInterface",
    "VMTemplate", "VMTemplateCreate",
    "StoragePool", "StoragePoolCreate", "Volume", "VolumeCreate",
    "ISOImage", "ISODownload", "CloudImage", "CloudImageDownload",
    "Network", "NetworkCreate", "NetworkDetail", "DHCPLease", "NetworkUpdate", "NetworkXML", "DHCPHost",
    "NetworkInterface", "NetworkConfig",
    "Task", "TaskCreate", "TaskUpdate",
    "HostInfo", "HostResources",
    "Cluster", "ClusterCreate", "ClusterScale", "ClusterNodeOut", "ClusterCommandOutput",
]
