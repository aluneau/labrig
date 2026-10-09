"""Kubernetes cluster endpoints"""
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Cluster as ClusterModel
from app.schemas import Cluster, ClusterCommandOutput, ClusterCreate, ClusterScale, Task
from app.schemas.openshift import AddonRequest, MetalLBOptions, MetalLBScenario
from app.services.cluster_service import cluster_service

router = APIRouter()


def _get(db: Session, cluster_id: int) -> ClusterModel:
    cluster = cluster_service.get_cluster(db, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return cluster


def _bad_request(fn, *args):
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, RuntimeError, TimeoutError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=List[Cluster])
def list_clusters(db: Session = Depends(get_db)):
    return cluster_service.list_clusters(db)


@router.post("", response_model=Cluster, status_code=201)
def create_cluster(data: ClusterCreate, db: Session = Depends(get_db)):
    """Create a cluster; provisioning runs as a background task (see task_id)"""
    cluster = _bad_request(cluster_service.create_cluster, db, data)
    return cluster_service.get_cluster_dict(db, cluster.id)


@router.get("/{cluster_id}", response_model=Cluster)
def get_cluster(cluster_id: int, db: Session = Depends(get_db)):
    cluster = cluster_service.get_cluster_dict(db, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return cluster


@router.delete("/{cluster_id}")
def delete_cluster(cluster_id: int, db: Session = Depends(get_db)):
    """Delete the node VMs and their disks, their DHCP/DNS records and the cluster's own network"""
    _bad_request(cluster_service.delete_cluster, db, _get(db, cluster_id))
    return {"message": "Cluster deleted"}


@router.post("/{cluster_id}/workers", response_model=Task)
def add_workers(cluster_id: int, data: ClusterScale, db: Session = Depends(get_db)):
    return _bad_request(cluster_service.add_workers, db, _get(db, cluster_id), data.count)


@router.delete("/{cluster_id}/nodes/{node_name}", response_model=Task)
def remove_node(cluster_id: int, node_name: str, db: Session = Depends(get_db)):
    """Drain and delete a worker from Kubernetes, then delete its VM"""
    return _bad_request(cluster_service.remove_node, db, _get(db, cluster_id), node_name)


@router.get("/{cluster_id}/kubeconfig", response_class=PlainTextResponse)
def get_kubeconfig(cluster_id: int, db: Session = Depends(get_db)):
    cluster = _get(db, cluster_id)
    text = _bad_request(cluster_service.get_kubeconfig, db, cluster)
    return PlainTextResponse(text, media_type="application/yaml", headers={
        "Content-Disposition": f'attachment; filename="{cluster.name}-kubeconfig.yaml"'})


@router.get("/{cluster_id}/kubectl/{view}", response_model=ClusterCommandOutput)
def kubectl(cluster_id: int, view: Literal["nodes", "pods"], db: Session = Depends(get_db)):
    """Output of `kubectl get nodes|pods` run on the first control plane (through the guest agent)"""
    return _bad_request(cluster_service.kubectl, db, _get(db, cluster_id), view)


@router.get("/{cluster_id}/metallb", response_model=MetalLBScenario)
def metallb(cluster_id: int, db: Session = Depends(get_db)):
    """MetalLB lab (OpenShift add-on or kubeadm): pool, service IP, announcing node / BGP next hops, checks"""
    cluster = _get(db, cluster_id)
    if cluster.type == "openshift":
        from app.services.openshift_installer import openshift_installer
        return _bad_request(openshift_installer.metallb_scenario, db, cluster)
    if cluster.type != "kubeadm":
        raise HTTPException(status_code=400, detail=f"MetalLB is managed for kubeadm and OpenShift clusters, not {cluster.type}")
    from app.services import kubeadm_metallb
    return _bad_request(kubeadm_metallb.scenario, db, cluster)


@router.put("/{cluster_id}/metallb", response_model=Task)
def set_metallb(cluster_id: int, data: MetalLBOptions, db: Session = Depends(get_db)):
    """Enable MetalLB, switch l2 <-> bgp, BFD, deploy / remove the demo (kubeadm), or disable it (kubeadm).
    OpenShift: same as POST /clusters/{id}/openshift/addons {kind: metallb} (can't be disabled)."""
    cluster = _get(db, cluster_id)
    if cluster.type == "openshift":
        if not data.enabled:
            raise HTTPException(status_code=400, detail="MetalLB can't be removed from an OpenShift cluster")
        from app.services.openshift_installer import openshift_installer
        return _bad_request(openshift_installer.add_addon, db, cluster, AddonRequest(kind="metallb", metallb=data))
    from app.services import kubeadm_metallb
    return _bad_request(kubeadm_metallb.set_options, db, cluster, data)


# Last: /{cluster_id}/{action} would otherwise shadow /{cluster_id}/workers
@router.post("/{cluster_id}/{action}", response_model=Task)
def cluster_action(cluster_id: int, action: Literal["start", "stop"], db: Session = Depends(get_db)):
    """Start (control planes first, waits for all nodes Ready) or stop (ACPI, workers first)"""
    cluster = _get(db, cluster_id)
    fn = cluster_service.start_cluster if action == "start" else cluster_service.stop_cluster
    return _bad_request(fn, db, cluster)
