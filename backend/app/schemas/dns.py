"""Host DNS resolver schemas

Custom records a user adds to the host DNS resolver (services/dns_service.py): an A record
(name -> IPv4) or an alias (name -> CNAME target). Mirrors router DNS records (schemas/group.py
DNSRecord), but these names are absolute FQDNs (the host resolver has no group domain to be relative to).
"""
import ipaddress
import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Same shape as schemas/group.py DNS_NAME, with an optional leading wildcard label
DNS_NAME = re.compile(r"^(\*\.)?([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.)*"
                      r"[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\.?$")


class HostDnsRecordCreate(BaseModel):
    """Add a custom record. Set `ip` for an A record, or `cname` for an alias (exactly one)."""
    name: str = Field(..., min_length=1, max_length=253)
    ip: Optional[str] = None
    cname: Optional[str] = None

    @field_validator("name", "cname")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip().rstrip(".").lower()
            if not DNS_NAME.match(v):
                raise ValueError(f"'{v}' is not a valid DNS name")
        return v

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        try:
            return str(ipaddress.IPv4Address(v.strip()))
        except ValueError:
            raise ValueError(f"'{v}' is not an IPv4 address")

    @model_validator(mode="after")
    def _one_value(self):
        if (self.ip is None) == (self.cname is None):
            raise ValueError("set 'ip' (an A record) or 'cname' (an alias), but not both")
        if self.cname and self.name.startswith("*."):
            raise ValueError("wildcards can only be A records")
        return self


class HostDnsRecordInfo(BaseModel):
    name: str
    ip: Optional[str] = None
    cname: Optional[str] = None
    type: str  # "A" | "CNAME"
