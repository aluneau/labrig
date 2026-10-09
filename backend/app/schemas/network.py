"""Network schemas"""
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class NetworkBase(BaseModel):
    """Base network schema"""
    name: str
    type: str = "nat"  # nat, bridge, isolated, macvtap
    domain: Optional[str] = None
    ip_address: Optional[str] = None
    prefix: Optional[int] = None
    dhcp_enabled: bool = True
    dhcp_start: Optional[str] = None
    dhcp_end: Optional[str] = None
    forward_mode: str = "nat"
    forward_dev: Optional[str] = None
    # SR-IOV VF pools (forward_mode hostdev) only: VLAN tag the PF applies to every VF of the pool
    # (<vlan><tag id/></vlan>; the guest sees untagged traffic). A NIC can override it.
    vlan: Optional[int] = Field(None, ge=1, le=4094)
    autostart: bool = False


class NetworkCreate(NetworkBase):
    """Network creation schema"""
    xml_config: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class Network(NetworkBase):
    """Network response schema"""
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    uuid: Optional[str] = None
    bridge_name: Optional[str] = None
    netmask: Optional[str] = None
    active: bool = False
    persistent: bool = True
    xml_config: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime


class NetworkList(BaseModel):
    """Network list response"""
    items: List[Network]
    total: int


class DHCPLease(BaseModel):
    """DHCP lease info"""
    ip_address: str
    mac_address: str
    hostname: Optional[str] = None
    expiry: Optional[datetime] = None
    clientid: Optional[str] = None


class NetworkDetail(Network):
    """Network detail with leases"""
    leases: List[DHCPLease] = []


class NetworkUpdate(BaseModel):
    """Editable network settings (applied by redefining the network)"""
    forward_mode: str = Field("nat", pattern=r"^(nat|route|open|isolated)$")
    forward_dev: Optional[str] = None
    domain: Optional[str] = None
    ip_address: Optional[str] = None
    prefix: Optional[int] = Field(None, ge=8, le=30)
    dhcp_enabled: bool = True
    dhcp_start: Optional[str] = None
    dhcp_end: Optional[str] = None
    restart: bool = True  # restart now if active, otherwise changes apply at next start


class NetworkXML(BaseModel):
    xml: str
    restart: bool = True


class DHCPHost(BaseModel):
    """Static DHCP reservation"""
    mac: str = Field(..., pattern=r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
    ip: str
    name: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$")


class NetworkInterface(BaseModel):
    vm: str
    mac: str
    active: bool = False  # the VM is running


class LeaseRelease(BaseModel):
    """Result of releasing a DHCP lease"""
    mac: str
    ip: str
    released: bool  # the lease is gone from libvirt's lease list


class NetworkConfig(BaseModel):
    """Everything the network edit page needs"""
    network: "NetworkDetail"
    hosts: List[DHCPHost] = []
    interfaces: List[NetworkInterface] = []
    xml: str
