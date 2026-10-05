"""Kubernetes cluster models

A cluster is a set of node VMs (libvirt is the source of truth: each node VM
carries <vmm:cluster .../> metadata, see cluster_service) plus what only the
app knows: the join token, the kubeconfig and the creation spec.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class Cluster(Base):
    __tablename__ = "clusters"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), unique=True, index=True, nullable=False)
    type = Column(String(20), nullable=False, default="k3s")  # k3s | kubeadm (openshift later)
    version = Column(String(64), nullable=True)  # requested, then the installed one
    network = Column(String(255), nullable=False)  # libvirt network the nodes are on
    network_owned = Column(Boolean, default=True)  # created for this cluster (deleted with it)
    domain = Column(String(255), nullable=False)  # base domain: api.<name>.<domain>
    # Lab group the nodes live in (kubeadm): its router serves their leases / DNS and the API load
    # balancer. Not a foreign key (groups are rebuilt from libvirt too); group_owned = auto-created
    # for this cluster and deleted with it
    group_id = Column(Integer, nullable=True, index=True)
    group_owned = Column(Boolean, nullable=True)
    api_ip = Column(String(64), nullable=True)
    spec = Column(JSON, nullable=True)  # creation request (without secrets)
    token = Column(String(255), nullable=True)  # join token: never returned by the API
    kubeconfig = Column(Text, nullable=True)
    # provisioning | ready | starting | stopping | stopped | error
    status = Column(String(20), default="provisioning")
    # Rebuilt from libvirt metadata: forgotten once none of its node VMs is left (see Group.adopted)
    adopted = Column(Boolean, nullable=True)
    status_message = Column(Text, nullable=True)
    task_id = Column(Integer, nullable=True)  # last background task (create / start / scale)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    nodes = relationship("ClusterNode", back_populates="cluster", cascade="all, delete-orphan",
                         order_by="ClusterNode.id")


class ClusterNode(Base):
    __tablename__ = "cluster_nodes"

    id = Column(Integer, primary_key=True, index=True)
    cluster_id = Column(Integer, ForeignKey("clusters.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), unique=True, nullable=False)  # = VM name = hostname
    role = Column(String(20), nullable=False)  # ctlplane | worker
    mac = Column(String(17), nullable=True)
    ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    cluster = relationship("Cluster", back_populates="nodes")
