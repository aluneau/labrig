"""Bare-metal host models

A bare-metal host is a physical server (Dell, Supermicro...) driven through its BMC (Redfish): power,
one-time boot and virtual media (services/bmc.py). Unlike a VM, libvirt knows nothing about it, so the
database is the source of truth here. The BMC password is NOT stored in this row; it lives in a 0600 file
under DATA_DIR/bmc (bmc_service), never returned by the API.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, JSON, String, Text

from app.database import Base


class BareMetalHost(Base):
    __tablename__ = "bare_metal_hosts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), unique=True, index=True, nullable=False)
    # Redfish base, e.g. https://10.6.75.161 (scheme optional on input, stored normalised)
    bmc_address = Column(String(255), nullable=False)
    bmc_username = Column(String(128), nullable=False)
    # bmc_password is never here: DATA_DIR/bmc/<id>.secret (0600)
    # MAC of the NIC the host provisions / boots on (matches the agent-config host entry)
    boot_mac = Column(String(17), nullable=True)
    # Last inventory snapshot from the BMC (model, cpu, ram, nics...), refreshed on demand
    inventory = Column(JSON, nullable=True)
    # available | provisioning | provisioned | error
    status = Column(String(20), default="available", nullable=False)
    status_message = Column(Text, nullable=True)
    # Cluster this host backs, set while it is a node (cluster name; freed on delete)
    cluster_name = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
