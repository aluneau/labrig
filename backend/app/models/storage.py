"""Storage models"""
from sqlalchemy import Column, Integer, String, BigInteger, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


class StoragePool(Base):
    """Storage Pool model"""
    __tablename__ = "storage_pools"
    
    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String(36), unique=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    type = Column(String(50), default="dir")  # dir, logical, iscsi, etc.
    path = Column(String(512), nullable=True)
    
    # Capacity
    capacity = Column(BigInteger, default=0)  # bytes
    allocation = Column(BigInteger, default=0)  # bytes
    available = Column(BigInteger, default=0)  # bytes
    
    # State
    state = Column(String(20), default="inactive")  # active, inactive
    autostart = Column(Boolean, default=False)
    
    # XML configuration
    xml_config = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    volumes = relationship("Volume", back_populates="pool")


class Volume(Base):
    """Storage Volume model"""
    __tablename__ = "volumes"
    
    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String(36), unique=True, index=True)
    name = Column(String(255), nullable=False)
    
    # Pool reference
    pool_id = Column(Integer, ForeignKey("storage_pools.id"), nullable=False)
    
    # VM reference (optional - volumes can exist without being attached)
    vm_id = Column(Integer, ForeignKey("vms.id"), nullable=True)
    
    # Volume properties
    type = Column(String(20), default="file")  # file, block, network, etc.
    format = Column(String(20), default="qcow2")  # qcow2, raw, vmdk, etc.
    
    # Size
    capacity = Column(BigInteger, default=0)  # bytes
    allocation = Column(BigInteger, default=0)  # bytes
    
    # Path to the volume
    path = Column(String(512), nullable=True)
    
    # Source (for cloud images)
    source_url = Column(String(1024), nullable=True)
    source_type = Column(String(50), nullable=True)  # cloud-image, iso, existing
    
    # Boot order
    boot_order = Column(Integer, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    pool = relationship("StoragePool", back_populates="volumes")
    vm = relationship("VM", back_populates="volumes")


class ISOImage(Base):
    """ISO Image model"""
    __tablename__ = "iso_images"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    filename = Column(String(255), nullable=False)
    
    # Path to the ISO
    path = Column(String(512), nullable=False)
    
    # Size
    size = Column(BigInteger, default=0)  # bytes
    
    # Metadata
    description = Column(Text, nullable=True)
    os_type = Column(String(50), nullable=True)
    
    # Checksum
    checksum = Column(String(128), nullable=True)
    checksum_type = Column(String(20), default="sha256")
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CloudImage(Base):
    """Cloud Image model (for ready-to-use cloud images)"""
    __tablename__ = "cloud_images"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    
    # Distribution info
    distribution = Column(String(50), nullable=False)  # ubuntu, centos, debian, fedora, etc.
    version = Column(String(50), nullable=False)  # 22.04, 9-stream, etc.
    arch = Column(String(20), default="x86_64")
    
    # Source
    url = Column(String(1024), nullable=False)
    
    # Local path
    path = Column(String(512), nullable=True)
    
    # Size
    size = Column(BigInteger, default=0)
    
    # Checksum
    checksum = Column(String(128), nullable=True)
    checksum_type = Column(String(20), default="sha256")
    checksum_url = Column(String(1024), nullable=True)
    
    # Status
    status = Column(String(20), default="pending")  # pending, downloading, ready, error
    download_progress = Column(Integer, default=0)  # percentage
    
    # Metadata
    description = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
