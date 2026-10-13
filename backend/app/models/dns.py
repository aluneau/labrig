"""Host DNS resolver models

User-managed custom records served by the host DNS resolver (services/dns_service.py), alongside the
api / *.apps zones it generates for groupless / bare-metal OpenShift clusters. A record is either an A
record (name -> IPv4) or an alias (name -> CNAME target). Point a client's DNS at the host to resolve
them. The host resolver is the source of truth for these (unlike cluster zones, which come from the DB's
clusters); they live in this small table so they survive restarts.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.database import Base


class HostDnsRecord(Base):
    __tablename__ = "host_dns_records"

    id = Column(Integer, primary_key=True, index=True)
    # FQDN, stored lowercased without a trailing dot; unique across the resolver
    name = Column(String(253), unique=True, index=True, nullable=False)
    ip = Column(String(45), nullable=True)      # IPv4 for an A record
    cname = Column(String(253), nullable=True)  # target for an alias (CNAME)
    created_at = Column(DateTime, default=datetime.utcnow)
