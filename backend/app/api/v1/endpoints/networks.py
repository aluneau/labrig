"""Network endpoints"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    Network, NetworkCreate, NetworkDetail, DHCPLease, NetworkUpdate, NetworkXML, DHCPHost, NetworkConfig,
)
from app.services.network_service import network_service

router = APIRouter()


class AutostartUpdate(BaseModel):
    autostart: bool


@router.get("", response_model=List[Network])
def list_networks(db: Session = Depends(get_db)):
    """List all networks"""
    return network_service.list_networks(db)


@router.get("/{network_id}", response_model=NetworkDetail)
def get_network(network_id: int, db: Session = Depends(get_db)):
    """Get network by ID, with DHCP leases"""
    network = network_service.get_network(db, network_id)
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    detail = NetworkDetail.model_validate(network)
    detail.leases = [DHCPLease(**lease) for lease in network_service.get_leases(db, network_id)]
    return detail


@router.post("", response_model=Network, status_code=201)
def create_network(net_data: NetworkCreate, db: Session = Depends(get_db)):
    """Create and start a new network"""
    try:
        return network_service.create_network(db, net_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{network_id}")
def delete_network(network_id: int, db: Session = Depends(get_db)):
    if not network_service.delete_network(db, network_id):
        raise HTTPException(status_code=404, detail="Network not found")
    return {"message": "Network deleted successfully"}


@router.post("/{network_id}/start", response_model=Network)
def start_network(network_id: int, db: Session = Depends(get_db)):
    network = network_service.set_active(db, network_id, True)
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    return network


@router.post("/{network_id}/stop", response_model=Network)
def stop_network(network_id: int, db: Session = Depends(get_db)):
    network = network_service.set_active(db, network_id, False)
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    return network


@router.put("/{network_id}/autostart", response_model=Network)
def set_network_autostart(network_id: int, data: AutostartUpdate, db: Session = Depends(get_db)):
    network = network_service.set_autostart(db, network_id, data.autostart)
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    return network


@router.get("/{network_id}/leases", response_model=List[DHCPLease])
def get_network_leases(network_id: int, db: Session = Depends(get_db)):
    if not network_service.get_network(db, network_id):
        raise HTTPException(status_code=404, detail="Network not found")
    return network_service.get_leases(db, network_id)


@router.get("/{network_id}/config", response_model=NetworkConfig)
def get_network_config(network_id: int, db: Session = Depends(get_db)):
    """Network with leases, static DHCP hosts, attached VM interfaces and raw XML"""
    config = network_service.get_config(db, network_id)
    if not config:
        raise HTTPException(status_code=404, detail="Network not found")
    return config


@router.put("/{network_id}", response_model=Network)
def update_network(network_id: int, data: NetworkUpdate, db: Session = Depends(get_db)):
    """Edit subnet, DHCP range, forward mode and domain"""
    try:
        network = network_service.update_network(db, network_id, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    return network


@router.put("/{network_id}/xml", response_model=Network)
def replace_network_xml(network_id: int, data: NetworkXML, db: Session = Depends(get_db)):
    """Replace the network definition with raw libvirt XML"""
    try:
        network = network_service.replace_xml(db, network_id, data.xml, data.restart)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not network:
        raise HTTPException(status_code=404, detail="Network not found")
    return network


@router.post("/{network_id}/hosts", status_code=201)
def add_dhcp_host(network_id: int, host: DHCPHost, db: Session = Depends(get_db)):
    """Add a static DHCP reservation (applied live, no restart)"""
    try:
        if not network_service.set_dhcp_host(db, network_id, host):
            raise HTTPException(status_code=404, detail="Network not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": "Reservation added"}


@router.put("/{network_id}/hosts/{mac}")
def update_dhcp_host(network_id: int, mac: str, host: DHCPHost, db: Session = Depends(get_db)):
    """Replace the reservation of `mac`"""
    try:
        if not network_service.set_dhcp_host(db, network_id, host, replace_mac=mac):
            raise HTTPException(status_code=404, detail="Reservation not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": "Reservation updated"}


@router.delete("/{network_id}/hosts/{mac}")
def delete_dhcp_host(network_id: int, mac: str, db: Session = Depends(get_db)):
    if not network_service.delete_dhcp_host(db, network_id, mac):
        raise HTTPException(status_code=404, detail="Reservation not found")
    return {"message": "Reservation removed"}
