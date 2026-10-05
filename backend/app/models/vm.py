"""VM models"""
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


class VM(Base):
    """Virtual Machine model"""
    __tablename__ = "vms"
    
    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String(36), unique=True, index=True)
    name = Column(String(255), unique=True, index=True, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), default="defined")  # defined, running, paused, shutoff, crashed
    
    # Resources
    memory = Column(Integer, default=2048)  # MB
    vcpu = Column(Integer, default=2)
    
    # XML configuration (libvirt domain XML)
    xml_config = Column(Text, nullable=True)
    
    # Metadata
    os_type = Column(String(50), nullable=True)
    arch = Column(String(20), default="x86_64")
    
    # Template reference
    template_id = Column(Integer, ForeignKey("vm_templates.id"), nullable=True)
    
    # Cloud-init
    cloudinit_config = Column(JSON, nullable=True)

    # One-shot boot order for the next start through the app, e.g. "cdrom,hd" (None = persistent order)
    next_boot = Column(String(64), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    template = relationship("VMTemplate", back_populates="vms")
    volumes = relationship("Volume", back_populates="vm")


class VMTemplate(Base):
    """VM Template model"""
    __tablename__ = "vm_templates"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    
    # Default resources
    memory = Column(Integer, default=2048)
    vcpu = Column(Integer, default=2)
    
    # Base XML configuration
    xml_config = Column(Text, nullable=True)
    
    # Cloud-init defaults
    cloudinit_defaults = Column(JSON, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    vms = relationship("VM", back_populates="template")
