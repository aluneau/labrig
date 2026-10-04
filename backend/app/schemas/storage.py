"""Storage schemas"""
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
from datetime import datetime


class StoragePoolBase(BaseModel):
    """Base storage pool schema"""
    name: str
    type: str = "dir"
    path: Optional[str] = None
    autostart: bool = True


class StoragePoolCreate(StoragePoolBase):
    """Storage pool creation schema"""
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    path: str = Field(..., min_length=2, pattern=r"^/")


class StoragePool(StoragePoolBase):
    """Storage pool response schema"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    uuid: Optional[str] = None
    capacity: int = 0
    allocation: int = 0
    available: int = 0
    state: str = "inactive"
    created_at: datetime
    updated_at: datetime


class VolumeCreate(BaseModel):
    """Volume creation schema"""
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    format: str = "qcow2"
    capacity: int = Field(..., gt=0)  # bytes


class Volume(BaseModel):
    """Volume response schema"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    pool_id: int
    pool_name: Optional[str] = None
    vm_id: Optional[int] = None
    type: str = "file"
    format: Optional[str] = None
    capacity: int = 0
    allocation: int = 0
    path: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ISOImage(BaseModel):
    """ISO image (an .iso volume in any active pool)"""
    name: str
    pool_name: str
    path: str
    size: int


class ISODownload(BaseModel):
    """Download an ISO from a URL into the default pool"""
    url: str = Field(..., pattern=r"^https?://")
    name: Optional[str] = None


class CloudImageDownload(BaseModel):
    """Download a cloud image; url defaults to the known one for distribution/version"""
    distribution: str = Field(..., min_length=1, max_length=50)
    version: str = Field(..., min_length=1, max_length=50)
    url: Optional[str] = Field(None, pattern=r"^https?://")
    description: Optional[str] = None


class CloudImage(BaseModel):
    """Cloud image response schema"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    distribution: str
    version: str
    arch: str = "x86_64"
    url: str
    path: Optional[str] = None
    size: int = 0
    status: str = "pending"  # downloading, ready, error
    download_progress: int = 0
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime
