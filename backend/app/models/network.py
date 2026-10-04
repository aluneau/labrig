"""Network models"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, JSON
from datetime import datetime

from app.database import Base


class Network(Base):
    """Network model"""
    __tablename__ = "networks"
    
    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String(36), unique=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    
    # Network type
    type = Column(String(50), default="nat")  # nat, bridge, isolated, macvtap
    
    # Bridge name (for bridged networks)
    bridge_name = Column(String(50), nullable=True)
    
    # IP configuration
    domain = Column(String(255), nullable=True)
    ip_address = Column(String(50), nullable=True)
    netmask = Column(String(50), nullable=True)
    prefix = Column(Integer, nullable=True)  # CIDR prefix (e.g., 24 for /24)
    
    # DHCP
    dhcp_enabled = Column(Boolean, default=True)
    dhcp_start = Column(String(50), nullable=True)
    dhcp_end = Column(String(50), nullable=True)
    
    # Forwarding (for NAT networks)
    forward_mode = Column(String(20), default="nat")  # nat, route, open
    forward_dev = Column(String(50), nullable=True)  # Interface to forward to
    
    # State
    active = Column(Boolean, default=False)
    autostart = Column(Boolean, default=False)
    persistent = Column(Boolean, default=True)
    
    # XML configuration
    xml_config = Column(Text, nullable=True)
    
    # Additional config (DNS, routes, etc.)
    config = Column(JSON, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
