"""Kubernetes cluster endpoints"""
from typing import List, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Cluster as ClusterModel
from app.schemas import Cluster, ClusterCommandOutput, ClusterCreate, ClusterScale, Task
from app.schemas.openshift import AcmImportRequest, AcmSpokeRequest, AcmStatus, AddonRequest, MetalLBOptions, MetalLBScenario
from app.schemas.cluster import ClusterMirrorRequest
from app.services import kube_exec
from app.services.acm import acm_service
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


@router.post("/{cluster_id}/mirror", response_model=Task)
def mirror_images(cluster_id: int, data: ClusterMirrorRequest, db: Session = Depends(get_db)):
    """Disconnected kubeadm cluster: copy more images into the group's mirror registry (task). The list of
    mirrored images is the cluster's `registry.images`."""
    return _bad_request(cluster_service.mirror_images, db, _get(db, cluster_id), data.images)


@router.get("/{cluster_id}/kubeconfig", response_class=PlainTextResponse)
def get_kubeconfig(cluster_id: int, db: Session = Depends(get_db)):
    cluster = _get(db, cluster_id)
    text = _bad_request(cluster_service.get_kubeconfig, db, cluster)
    return PlainTextResponse(text, media_type="application/yaml", headers={
        "Content-Disposition": f'attachment; filename="{cluster.name}-kubeconfig.yaml"'})


def _openshift(db: Session, cluster_id: int) -> ClusterModel:
    cluster = _get(db, cluster_id)
    if cluster.type != "openshift":
        raise HTTPException(status_code=400, detail="Only OpenShift clusters have oc output")
    return cluster


@router.get("/{cluster_id}/oc-output")
def list_oc_output(cluster_id: int, db: Session = Depends(get_db)):
    """Files written by the MCP oc tool (adm must-gather / inspect tarballs, oc cp / rsync) in the install dir"""
    return kube_exec.list_outputs(_openshift(db, cluster_id))


@router.get("/{cluster_id}/oc-output/{name}")
def get_oc_output(cluster_id: int, name: str, db: Session = Depends(get_db)):
    path = _bad_request(kube_exec.output_path, _openshift(db, cluster_id), name)
    if not path.is_file():
        raise HTTPException(status_code=400, detail=f"{name} is a directory")
    return FileResponse(path, filename=name, media_type="application/gzip" if name.endswith(".gz")
                        else "application/octet-stream")


@router.delete("/{cluster_id}/oc-output/{name}")
def delete_oc_output(cluster_id: int, name: str, db: Session = Depends(get_db)):
    _bad_request(kube_exec.delete_output, _openshift(db, cluster_id), name)
    return {"deleted": name}


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


@router.get("/{cluster_id}/acm", response_model=AcmStatus)
def acm_status(cluster_id: int, db: Session = Depends(get_db)):
    """ACM hub (OpenShift add-on `acm`): MultiClusterHub phase, console link, managed clusters with their
    ManagedCluster conditions (Joined / Available), clusters of the app that can be imported"""
    cluster = _get(db, cluster_id)
    if cluster.type != "openshift":
        raise HTTPException(status_code=400, detail=f"{cluster.name} is a {cluster.type} cluster: an ACM hub is OpenShift")
    return _bad_request(acm_service.status, db, cluster)


@router.post("/{cluster_id}/acm/managed", response_model=Task)
def acm_import(cluster_id: int, data: AcmImportRequest, db: Session = Depends(get_db)):
    """Import another cluster of the app (kubeadm, k3s, OpenShift) into the ACM hub `cluster_id` (body cluster_id = the
    cluster to import; a task on the hub): ManagedCluster +
    KlusterletAddonConfig, then auto-import-secret (when the hub reaches its API) or the import manifests applied
    by the app; waits for Joined + Available. Fails in plain words when the managed cluster can't reach the hub."""
    return _bad_request(acm_service.import_cluster, db, _get(db, cluster_id), data.cluster_id, data.method)


@router.delete("/{cluster_id}/acm/managed/{name}", response_model=Task)
def acm_detach(cluster_id: int, name: str, db: Session = Depends(get_db)):
    """Detach the managed cluster `name` from the hub `cluster_id` (task on the hub): deletes its ManagedCluster, the
    hub removes the klusterlet; a cluster that no longer reports gets its leftover finalizers removed"""
    return _bad_request(acm_service.detach, db, _get(db, cluster_id), name)


@router.post("/{cluster_id}/acm/spokes", response_model=Cluster, status_code=201)
def acm_spoke_create(cluster_id: int, data: AcmSpokeRequest, db: Session = Depends(get_db)):
    """The ACM hub provisions a single-node OpenShift on a BMC member of its lab group. Returns the new cluster
    (type openshift, `spec.openshift.provisioner` = this hub; its task_id follows the install): metal3 + assisted
    service on the hub, ClusterDeployment / AgentClusterInstall / InfraEnv / BareMetalHost (Redfish virtual media
    through the app's BMC emulator), then the ManagedCluster. Kubeconfig, logs, oc: the usual cluster endpoints;
    DELETE /clusters/{id} deletes it through the hub (the machine stays)."""
    from app.services.acm_spokes import spoke_service
    net = data.net.model_dump() if data.net is not None else None
    cluster = _bad_request(spoke_service.provision, db, _get(db, cluster_id), data.name, data.machine, data.version,
                           data.host_id, net)
    return cluster_service.get_cluster_dict(db, cluster.id)


@router.get("/{cluster_id}/acm/siteconfig")
def acm_siteconfig(cluster_id: int, db: Session = Depends(get_db)):
    """The GitOps artifact for a real bare-metal spoke: an ACM SiteConfig `ClusterInstance` manifest (+ the BMC
    Secret stub, password not embedded) to commit to git for Argo CD / the SiteConfig operator. The app applies
    the expanded low-level CRs directly; this is the same spoke expressed the way a fleet is defined."""
    from app.services.acm_spokes import spoke_service
    manifest = _bad_request(spoke_service.siteconfig_manifest, db, _get(db, cluster_id))
    return {"manifest": manifest}


# Last: /{cluster_id}/{action} would otherwise shadow /{cluster_id}/workers
@router.post("/{cluster_id}/{action}", response_model=Task)
def cluster_action(cluster_id: int, action: Literal["start", "stop"], db: Session = Depends(get_db)):
    """Start (control planes first, waits for all nodes Ready) or stop (ACPI, workers first)"""
    cluster = _get(db, cluster_id)
    fn = cluster_service.start_cluster if action == "start" else cluster_service.stop_cluster
    return _bad_request(fn, db, cluster)
