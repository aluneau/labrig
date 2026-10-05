"""Lab group endpoints (future-features §2.4)"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import Task
from app.schemas.group import (
    DNSRecord, Group, GroupCreateResult, GroupDetail, GroupExport, GroupSpec, MemberSpec, RouterConfig,
)
from app.services.group_service import group_service

router = APIRouter()


def _group_or_404(db: Session, group_id: int):
    group = group_service.get_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


def _detail(db: Session, group_id: int):
    return group_service.detail_api(db, _group_or_404(db, group_id))


@router.get("", response_model=List[Group])
def list_groups(db: Session = Depends(get_db)):
    """Groups with live state (synced from libvirt metadata)"""
    return group_service.list_api(db)


@router.post("/sync")
def sync_groups(db: Session = Depends(get_db)):
    """Rebuild missing groups from libvirt metadata"""
    group_service.sync_groups(db)
    return {"message": "Groups synced"}


@router.post("", response_model=GroupCreateResult, status_code=202)
def create_group(spec: GroupSpec, db: Session = Depends(get_db)):
    """Create network + router + members. Runs as a task (router first boot takes a few minutes)."""
    try:
        group, task = group_service.create_group(db, spec)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"group": group_service.to_api(db, group), "task_id": task.id}


@router.get("/{group_id}", response_model=GroupDetail)
def get_group(group_id: int, db: Session = Depends(get_db)):
    """Group with live state, router uplink IPs and the router's DHCP leases"""
    return _detail(db, group_id)


@router.put("/{group_id}", response_model=GroupDetail)
def update_group(
    group_id: int, spec: GroupSpec,
    replace_members: bool = Query(False, description="Recreate members whose image/size/cloud-init changed"),
    delete_disks: bool = Query(True, description="Delete the disks of removed members"),
    db: Session = Depends(get_db),
):
    """Replace the spec: members are added/removed, the router config is re-rendered and applied live"""
    _group_or_404(db, group_id)
    try:
        group_service.update_group(db, group_id, spec, replace_members=replace_members, removed_disks=delete_disks)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _detail(db, group_id)


@router.delete("/{group_id}")
def delete_group(
    group_id: int,
    delete_disks: bool = Query(True, description="Delete the disks of the router and members (False keeps them)"),
    db: Session = Depends(get_db),
):
    """Delete the group's VMs and network. Only libvirt objects tagged with this group are touched."""
    try:
        found = group_service.delete_group(db, group_id, delete_disks=delete_disks)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not found:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"message": "Group deleted"}


@router.post("/{group_id}/start", response_model=Task, status_code=202)
def start_group(group_id: int, db: Session = Depends(get_db)):
    """Start the router, wait for it, apply its config, then start the members (task)"""
    _group_or_404(db, group_id)
    try:
        return group_service.start_group(db, group_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{group_id}/stop", response_model=Task, status_code=202)
def stop_group(group_id: int, force: bool = Query(False), db: Session = Depends(get_db)):
    """Shut down the members, then the router (task). force=true powers them off."""
    _group_or_404(db, group_id)
    try:
        return group_service.stop_group(db, group_id, force=force)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{group_id}/members", response_model=GroupDetail, status_code=201)
def add_member(group_id: int, member: MemberSpec, db: Session = Depends(get_db)):
    """Add a member VM: gets a fixed MAC + reserved IP, the router is updated live"""
    _group_or_404(db, group_id)
    try:
        group_service.add_member(db, group_id, member)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _detail(db, group_id)


@router.delete("/{group_id}/members/{name}", response_model=GroupDetail)
def remove_member(group_id: int, name: str, delete_disks: bool = Query(True), db: Session = Depends(get_db)):
    _group_or_404(db, group_id)
    try:
        group_service.remove_member(db, group_id, name, delete_disks=delete_disks)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _detail(db, group_id)


@router.post("/{group_id}/dns-records", response_model=GroupDetail, status_code=201)
def set_dns_record(group_id: int, record: DNSRecord, db: Session = Depends(get_db)):
    """Add or replace a DNS record (applied live on the router)"""
    _group_or_404(db, group_id)
    try:
        group_service.set_dns_record(db, group_id, record)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _detail(db, group_id)


@router.delete("/{group_id}/dns-records/{name}", response_model=GroupDetail)
def remove_dns_record(group_id: int, name: str, db: Session = Depends(get_db)):
    _group_or_404(db, group_id)
    try:
        group_service.remove_dns_record(db, group_id, name)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _detail(db, group_id)


@router.get("/{group_id}/router/config", response_model=RouterConfig)
def router_config(group_id: int, db: Session = Depends(get_db)):
    """Rendered router config (cloud-init + spec-dependent files), for debugging"""
    try:
        return group_service.router_config(db, _group_or_404(db, group_id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{group_id}/router/apply", response_model=GroupDetail)
def apply_router_config(group_id: int, db: Session = Depends(get_db)):
    """Push the rendered config to the router again (guest agent)"""
    group = _group_or_404(db, group_id)
    try:
        if not group_service.push_router_config(db, group):
            raise HTTPException(status_code=409, detail=group.config_error)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _detail(db, group_id)


@router.get("/{group_id}/export", response_model=GroupExport)
def export_group(group_id: int, db: Session = Depends(get_db)):
    """The group spec as YAML (POST it back to /groups to recreate the lab)"""
    return group_service.export_yaml(_group_or_404(db, group_id))
