"""Kubernetes cluster schemas"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# Accepted by the API; only "k3s" is implemented for now (cluster_service.DRIVERS)
CLUSTER_TYPES = ["k3s", "kubeadm", "openshift"]


class NodeResources(BaseModel):
    memory: int = Field(2048, ge=512)  # MiB
    vcpu: int = Field(2, ge=1, le=64)
    disk_size: int = Field(20, ge=5, le=2048)  # GiB


class ClusterCreate(BaseModel):
    # A DNS label: also the prefix of node names and of the api.<name>.<domain> record
    name: str = Field(..., min_length=1, max_length=40, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
    type: str = "k3s"
    # k3s release (INSTALL_K3S_VERSION, e.g. v1.33.5+k3s1); empty = stable channel
    version: Optional[str] = Field(None, pattern=r"^v\d+\.\d+\.\d+(\+k3s\d+)?$")
    ctlplanes: int = Field(1, ge=1, le=5)  # 1, or 3/5 with embedded etcd
    workers: int = Field(2, ge=0, le=20)
    ctlplane: NodeResources = NodeResources()
    worker: NodeResources = NodeResources()
    # Ready cloud image for the nodes (Debian 13 / AlmaLinux 9...); default: the first suitable one
    cloud_image_id: Optional[int] = None
    domain: str = Field("lab", min_length=1, max_length=200, pattern=r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
    # Existing libvirt network to put the nodes on; None = create vmm-k-<name> (NAT)
    network: Optional[str] = None
    # Subnet of the created network (e.g. 10.43.5.0/24); None = first free /24 of CLUSTER_SUBNET_POOL
    cidr: Optional[str] = None
    # Kubernetes pod / service networks: must not overlap the node network
    pod_cidr: str = "10.244.0.0/16"
    service_cidr: str = "10.96.0.0/16"
    # Extra server flags appended to INSTALL_K3S_EXEC (e.g. "--disable traefik")
    extra_args: Optional[str] = Field(None, max_length=1000)
    # Login on the nodes (console / SSH)
    username: Optional[str] = Field("admin", pattern=r"^[a-z_][a-z0-9_-]*$")
    password: Optional[str] = None
    ssh_keys: List[str] = []
    keyboard: Optional[str] = Field(None, pattern=r"^[a-z]{2,10}$")


class ClusterScale(BaseModel):
    count: int = Field(1, ge=1, le=10)  # workers to add


class ClusterNodeOut(BaseModel):
    name: str
    role: str
    ip: Optional[str] = None
    mac: Optional[str] = None
    vm_id: Optional[int] = None
    state: str = "missing"  # libvirt domain state, "missing" if the VM is gone
    fqdn: Optional[str] = None


class Cluster(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str
    version: Optional[str] = None
    network: str
    network_owned: bool
    domain: str
    api_hostname: str
    api_ip: Optional[str] = None
    api_endpoint: Optional[str] = None
    status: str
    status_message: Optional[str] = None
    task_id: Optional[int] = None
    task_running: bool = False
    task_progress: Optional[int] = None
    has_kubeconfig: bool = False
    ctlplanes: int = 0
    workers: int = 0
    spec: Optional[Dict[str, Any]] = None
    nodes: List[ClusterNodeOut] = []
    created_at: datetime
    updated_at: datetime


class ClusterCommandOutput(BaseModel):
    command: str
    node: str
    exitcode: int
    stdout: str
    stderr: str
