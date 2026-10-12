"""Bare-metal host schemas (services/bmc.py, models/bmh.py)"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

_MAC = r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"


class BareMetalHostCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
    # Redfish address: "10.0.0.1", "https://10.0.0.1" or "https://10.0.0.1:443"
    bmc_address: str = Field(..., min_length=3, max_length=255)
    bmc_username: str = Field(..., min_length=1, max_length=128)
    bmc_password: str = Field(..., min_length=1, max_length=255)  # stored 0600, never returned
    # The provisioning NIC's MAC; if omitted, the first LinkUp NIC from the BMC inventory is used
    boot_mac: Optional[str] = Field(None, pattern=_MAC)


class BareMetalHostUpdate(BaseModel):
    bmc_address: Optional[str] = Field(None, min_length=3, max_length=255)
    bmc_username: Optional[str] = Field(None, min_length=1, max_length=128)
    bmc_password: Optional[str] = Field(None, min_length=1, max_length=255)
    boot_mac: Optional[str] = Field(None, pattern=_MAC)


class BareMetalHost(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    bmc_address: str
    bmc_username: str
    boot_mac: Optional[str] = None
    inventory: Optional[Dict[str, Any]] = None
    status: str
    status_message: Optional[str] = None
    cluster_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class BareMetalPower(BaseModel):
    # on | off | cycle
    action: str = Field(..., pattern=r"^(on|off|cycle)$")
    graceful: bool = False


class BareMetalHostNic(BaseModel):
    id: Optional[str] = None
    mac: str
    link: Optional[str] = None
    speed_mbps: Optional[int] = None
    ipv4: List[str] = []
