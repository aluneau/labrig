"""Lab group models

The group spec (JSON) is the source of truth for a group; the router config is
generated from it. Rows can be rebuilt from libvirt: the router domain carries
the spec in its <metadata>, members carry their group name and role.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), unique=True, nullable=False)
    cidr = Column(String(32), nullable=False)
    domain = Column(String(255), nullable=False)
    uplink = Column(String(255), nullable=True)  # libvirt network of the router's eth0, None = disconnected
    # Not foreign keys: VM rows come and go with libvirt (sync_vms); resolved by name
    router_vm_id = Column(Integer, nullable=True)
    router_vm_name = Column(String(255), nullable=True)
    spec = Column(JSON, nullable=False)
    # creating | ready | updating | error | deleting | missing (network gone from libvirt)
    status = Column(String(20), default="creating")
    # Rebuilt from libvirt metadata (created by another instance / before a DB loss): its spec lives in
    # libvirt, so the row is forgotten once nothing of it is left there (no "missing" record)
    adopted = Column(Boolean, nullable=True)
    error_message = Column(Text, nullable=True)
    # Router config pushed through the guest agent since the last spec change?
    config_applied = Column(Boolean, default=False)
    config_applied_at = Column(DateTime, nullable=True)
    config_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan",
                           order_by="GroupMember.id")


class GroupMember(Base):
    __tablename__ = "group_members"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(64), nullable=False)        # member name in the spec (also its hostname)
    vm_id = Column(Integer, nullable=True)
    vm_name = Column(String(255), nullable=True)     # libvirt domain: <group>-<member>
    role = Column(String(50), default="member")
    ip = Column(String(50), nullable=True)
    mac = Column(String(17), nullable=True)
    hostname = Column(String(255), nullable=True)

    group = relationship("Group", back_populates="members")
