"""Lab group endpoints (future-features §2.4)"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import Task
from app.schemas.group import (
    DNSRecord, Group, GroupCreateResult, GroupDetail, GroupExport, GroupSpec, MemberSpec, RouterConfig,
)
from app.schemas.group import DHCPHostSpec, GroupLease, LeaseRelease
from app.schemas.group import WireGuardPeerCreate, WireGuardPeerCreated, WireGuardSettings, WireGuardStatus
from app.schemas.group import BGPSettings, BGPStatus, GroupTopology
from app.services.group_service import LeaseInUse, group_service
from app.schemas.registry import MirrorRequest, MirrorStarted, RegistryStatus
from app.services import registry_service, topology_service

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


# Static DHCP reservations for non-member machines + the router's leases

@router.get("/{group_id}/dhcp-hosts", response_model=List[DHCPHostSpec])
def list_dhcp_hosts(group_id: int, db: Session = Depends(get_db)):
    return group_service.dhcp_hosts(_group_or_404(db, group_id))


@router.post("/{group_id}/dhcp-hosts", response_model=GroupDetail, status_code=201)
def add_dhcp_host(group_id: int, host: DHCPHostSpec, db: Session = Depends(get_db)):
    """Reserve an IP (and optionally <hostname>.<domain>) for a MAC; applied live on the router.
    A running machine gets the address when it renews its lease (or reboots)."""
    _group_or_404(db, group_id)
    try:
        group_service.set_dhcp_host(db, group_id, host)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _detail(db, group_id)


@router.put("/{group_id}/dhcp-hosts/{mac}", response_model=GroupDetail)
def update_dhcp_host(group_id: int, mac: str, host: DHCPHostSpec, db: Session = Depends(get_db)):
    """Replace the reservation of `mac` (the body may change the MAC too)"""
    _group_or_404(db, group_id)
    try:
        group_service.set_dhcp_host(db, group_id, host, replace_mac=mac)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _detail(db, group_id)


@router.delete("/{group_id}/dhcp-hosts/{mac}", response_model=GroupDetail)
def remove_dhcp_host(
    group_id: int, mac: str,
    release_lease: bool = Query(False, description="Also drop the MAC's current lease on the router"),
    db: Session = Depends(get_db),
):
    _group_or_404(db, group_id)
    try:
        group_service.remove_dhcp_host(db, group_id, mac, release_lease=release_lease)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _detail(db, group_id)


@router.get("/{group_id}/leases", response_model=List[GroupLease])
def list_leases(group_id: int, db: Session = Depends(get_db)):
    """The router's DHCP leases (read through the guest agent), marked member / reservation / dynamic"""
    return group_service.leases(_group_or_404(db, group_id))


@router.delete("/{group_id}/leases/{mac}", response_model=LeaseRelease)
def release_lease(group_id: int, mac: str, force: bool = Query(False), db: Session = Depends(get_db)):
    """Drop a lease on the router (409 if a running VM has this MAC, unless force)"""
    _group_or_404(db, group_id)
    try:
        return group_service.release_lease(db, group_id, mac, force=force)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except LeaseInUse as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


# WireGuard remote access (docs/wireguard.md)

@router.get("/{group_id}/wireguard", response_model=WireGuardStatus)
def wireguard_status(group_id: int, db: Session = Depends(get_db)):
    """Settings, host relay state and devices with their last handshake (read on the router)"""
    return group_service.wireguard_status(_group_or_404(db, group_id))


@router.put("/{group_id}/wireguard", response_model=WireGuardStatus)
def set_wireguard(group_id: int, body: WireGuardSettings, db: Session = Depends(get_db)):
    """Enable / disable remote access (applied live on the router; disabling keeps keys and devices)"""
    _group_or_404(db, group_id)
    try:
        group_service.set_wireguard(db, group_id, body.enabled, body.listen_port, body.host_port)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return group_service.wireguard_status(_group_or_404(db, group_id))


@router.post("/{group_id}/wireguard/peers", response_model=WireGuardPeerCreated, status_code=201)
def add_wireguard_peer(group_id: int, body: WireGuardPeerCreate, db: Session = Depends(get_db)):
    """Add a device. Without public_key the key pair is generated: the returned config holds the
    private key, which is not stored (download it now)."""
    _group_or_404(db, group_id)
    try:
        return group_service.add_wg_peer(db, group_id, body.name, body.public_key, body.endpoint_host, body.allowed_ips)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{group_id}/wireguard/peers/{name}/config", response_model=WireGuardPeerCreated)
def wireguard_peer_config(group_id: int, name: str, endpoint_host: Optional[str] = Query(None, max_length=253),
                          db: Session = Depends(get_db)):
    """The device's config again (no private key: add yours)"""
    try:
        return group_service.wg_peer_config(_group_or_404(db, group_id), name, endpoint_host)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{group_id}/wireguard/peers/{name}", response_model=WireGuardStatus)
def remove_wireguard_peer(group_id: int, name: str, db: Session = Depends(get_db)):
    _group_or_404(db, group_id)
    try:
        group_service.remove_wg_peer(db, group_id, name)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return group_service.wireguard_status(_group_or_404(db, group_id))


# BGP (docs/bgp.md) and the topology view

@router.get("/{group_id}/bgp", response_model=BGPStatus)
def bgp_status(group_id: int, db: Session = Depends(get_db)):
    """Settings + live sessions and BGP routes of the router (vtysh through the guest agent)"""
    return group_service.bgp_status(_group_or_404(db, group_id))


@router.put("/{group_id}/bgp", response_model=BGPStatus)
def set_bgp(group_id: int, body: BGPSettings, db: Session = Depends(get_db)):
    """Enable / configure / disable BGP on the router (applied live; an older router installs frr first)"""
    _group_or_404(db, group_id)
    try:
        group_service.set_bgp(db, group_id, body.model_dump(exclude_unset=True))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return group_service.bgp_status(_group_or_404(db, group_id))


@router.get("/{group_id}/topology", response_model=GroupTopology)
def group_topology(group_id: int, db: Session = Depends(get_db)):
    """Everything the topology diagram shows: router roles and addresses, machines with state and BGP
    session, clusters (MetalLB pool / service IP), WireGuard devices, BGP routes"""
    return topology_service.topology(db, _group_or_404(db, group_id))


# Mirror registry on the router + egress (docs/disconnected.md). Enable / disable / egress: spec PUT

@router.get("/{group_id}/registry", response_model=RegistryStatus)
def registry_status(group_id: int, db: Session = Depends(get_db)):
    """Registry state (live from the router), URLs, CA, disk usage, mirrored content"""
    return registry_service.status(db, _group_or_404(db, group_id))


@router.post("/{group_id}/registry/setup", response_model=MirrorStarted, status_code=202)
def registry_setup(group_id: int, db: Session = Depends(get_db)):
    """(Re)run the registry setup: restarts the router when it needs more RAM, installs Quay (task)"""
    group = _group_or_404(db, group_id)
    if not GroupSpec.model_validate(group.spec).router.registry.enabled:
        raise HTTPException(status_code=400, detail="Enable the mirror registry first (router.registry.enabled)")
    return {"task_id": registry_service.start_setup(db, group).id}


@router.post("/{group_id}/registry/mirror", response_model=MirrorStarted, status_code=202)
def registry_mirror(group_id: int, body: MirrorRequest, db: Session = Depends(get_db)):
    """Copy a release / operator packages / images into the registry with oc-mirror v2 (task)"""
    group = _group_or_404(db, group_id)
    try:
        return {"task_id": registry_service.start_mirror(db, group, body).id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{group_id}/registry/ca.crt", response_class=PlainTextResponse)
def registry_ca(group_id: int, db: Session = Depends(get_db)):
    """The registry's CA certificate (PEM)"""
    ca = registry_service.ca_pem(_group_or_404(db, group_id))
    if not ca:
        raise HTTPException(status_code=404, detail="The registry CA is not known yet (set the registry up first)")
    return PlainTextResponse(ca, media_type="application/x-pem-file")
