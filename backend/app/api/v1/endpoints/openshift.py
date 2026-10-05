"""OpenShift endpoints: pull secret, versions, operator catalog, and per-cluster views / add-ons"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Cluster as ClusterModel
from app.schemas import Task
from app.schemas.openshift import (AddonRequest, CatalogOperator, ClusterCredentials, InstalledOperator,
                                   InstallStatus, MetalLBScenario, OpenShiftChannel, OpenShiftVersion,
                                   PackageManifest, PullSecretIn, PullSecretStatus)
from app.services.cluster_service import cluster_service
from app.services.openshift_installer import openshift_installer
from app.services.openshift_service import openshift_service

router = APIRouter()        # /openshift
cluster_router = APIRouter()  # /clusters/{id}/openshift


def _bad_request(fn, *args):
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, RuntimeError, TimeoutError) as e:
        raise HTTPException(status_code=400, detail=str(e))


def _cluster(db: Session, cluster_id: int) -> ClusterModel:
    cluster = cluster_service.get_cluster(db, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    if cluster.type != "openshift":
        raise HTTPException(status_code=400, detail=f"{cluster.name} is a {cluster.type} cluster, not OpenShift")
    return cluster


@router.get("/pull-secret", response_model=PullSecretStatus)
def pull_secret_status():
    return openshift_service.pull_secret_status()


@router.put("/pull-secret", response_model=PullSecretStatus)
def set_pull_secret(data: PullSecretIn):
    """Store the pull secret (pasted JSON, or read from a host path such as ~/pull-secret.json)"""
    return _bad_request(openshift_service.set_pull_secret, data.content, data.path)


@router.delete("/pull-secret", response_model=PullSecretStatus)
def delete_pull_secret():
    return openshift_service.delete_pull_secret()


@router.get("/channels", response_model=List[OpenShiftChannel])
def channels():
    """Minors on mirror.openshift.com with their stable (or candidate) channel and latest release"""
    return _bad_request(openshift_service.channels)


@router.get("/versions", response_model=List[OpenShiftVersion])
def versions(channel: str = Query("stable-4.20")):
    """Releases of a channel (upgrade graph), newest first"""
    return _bad_request(openshift_service.versions, channel)


@router.get("/catalog", response_model=List[CatalogOperator])
def catalog():
    return openshift_service.catalog()


@cluster_router.get("/install-status", response_model=InstallStatus)
def install_status(cluster_id: int, db: Session = Depends(get_db)):
    return _bad_request(openshift_installer.install_status, db, _cluster(db, cluster_id))


@cluster_router.get("/credentials", response_model=ClusterCredentials)
def credentials(cluster_id: int, db: Session = Depends(get_db)):
    """kubeadmin password and console URL"""
    return openshift_installer.credentials(_cluster(db, cluster_id))


@cluster_router.get("/ssh-key", response_class=PlainTextResponse)
def ssh_key(cluster_id: int, db: Session = Depends(get_db)):
    """Private key of the nodes' `core` user"""
    cluster = _cluster(db, cluster_id)
    text = _bad_request(openshift_installer.ssh_key, cluster)
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{cluster.name}-id_ed25519"'})


@cluster_router.get("/operators", response_model=List[InstalledOperator])
def operators(cluster_id: int, db: Session = Depends(get_db)):
    return _bad_request(openshift_installer.operators, _cluster(db, cluster_id))


@cluster_router.get("/packagemanifests", response_model=List[PackageManifest])
def packagemanifests(cluster_id: int, db: Session = Depends(get_db)):
    return _bad_request(openshift_installer.packagemanifests, _cluster(db, cluster_id))


@cluster_router.post("/addons", response_model=Task)
def add_addon(cluster_id: int, data: AddonRequest, db: Session = Depends(get_db)):
    return _bad_request(openshift_installer.add_addon, db, _cluster(db, cluster_id), data)


@cluster_router.get("/metallb", response_model=MetalLBScenario)
def metallb(cluster_id: int, db: Session = Depends(get_db)):
    """State of the MetalLB L2 lab: pool, service IP, announcing node, endpoints, checks"""
    return _bad_request(openshift_installer.metallb_scenario, db, _cluster(db, cluster_id))
