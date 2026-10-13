"""Bare-metal host endpoints (BMC-driven physical servers; services/baremetal_service.py)"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    BareMetalHost, BareMetalHostCreate, BareMetalHostNic, BareMetalHostUpdate, BareMetalPower,
)
from app.services.bmc import BMCError
from app.services.baremetal_service import baremetal_service

router = APIRouter()


@router.get("", response_model=List[BareMetalHost])
def list_hosts(db: Session = Depends(get_db)):
    return baremetal_service.list_hosts(db)


@router.post("", response_model=BareMetalHost, status_code=201)
def create_host(data: BareMetalHostCreate, db: Session = Depends(get_db)):
    """Register a bare-metal host by its BMC. The password is stored 0600 and never returned; the BMC is
    probed once (status 'error' with the reason if it is unreachable or the credentials are wrong)."""
    try:
        return baremetal_service.create_host(db, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _host(db: Session, bmh_id: int):
    host = baremetal_service.get_host(db, bmh_id)
    if not host:
        raise HTTPException(status_code=404, detail="Bare-metal host not found")
    return host


@router.get("/{bmh_id}", response_model=BareMetalHost)
def get_host(bmh_id: int, db: Session = Depends(get_db)):
    return _host(db, bmh_id)


@router.put("/{bmh_id}", response_model=BareMetalHost)
def update_host(bmh_id: int, data: BareMetalHostUpdate, db: Session = Depends(get_db)):
    host = baremetal_service.update_host(db, bmh_id, data)
    if not host:
        raise HTTPException(status_code=404, detail="Bare-metal host not found")
    return host


@router.delete("/{bmh_id}")
def delete_host(bmh_id: int, db: Session = Depends(get_db)):
    try:
        if not baremetal_service.delete_host(db, bmh_id):
            raise HTTPException(status_code=404, detail="Bare-metal host not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"message": "Bare-metal host removed"}


@router.post("/{bmh_id}/refresh", response_model=BareMetalHost)
def refresh_host(bmh_id: int, db: Session = Depends(get_db)):
    """Re-read inventory and power state from the BMC."""
    host = baremetal_service.refresh(db, bmh_id)
    if not host:
        raise HTTPException(status_code=404, detail="Bare-metal host not found")
    return host


@router.get("/{bmh_id}/nics", response_model=List[BareMetalHostNic])
def host_nics(bmh_id: int, db: Session = Depends(get_db)):
    host = _host(db, bmh_id)
    try:
        return baremetal_service.nics(db, host)
    except BMCError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/{bmh_id}/console-screenshot")
def console_screenshot(bmh_id: int, db: Session = Depends(get_db)):
    """A PNG snapshot of the host's video console (Dell iDRAC only). 502 if the BMC can't provide one, so
    the UI can fall back to the BMC web console."""
    host = _host(db, bmh_id)
    try:
        img = baremetal_service.screenshot(db, host)
    except BMCError as e:
        raise HTTPException(status_code=502, detail=str(e))
    # Sniff the real format (Dell returns JPEG): JPEG starts FF D8, PNG starts 89 50 4E 47.
    media = "image/jpeg" if img[:2] == b"\xff\xd8" else "image/png"
    return Response(content=img, media_type=media, headers={"Cache-Control": "no-store"})


@router.post("/{bmh_id}/power")
def power_host(bmh_id: int, data: BareMetalPower, db: Session = Depends(get_db)):
    """Power the host on, off (optionally graceful) or cycle it, through the BMC."""
    host = _host(db, bmh_id)
    try:
        state = baremetal_service.power(db, host, data.action, graceful=data.graceful)
    except BMCError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"power_state": state}


@router.post("/{bmh_id}/reset", response_model=BareMetalHost)
def reset_host(bmh_id: int, wipe: bool = True, db: Session = Depends(get_db)):
    """Decommission a host: eject media, optionally wipe its disks (wipe=true, Dell iDRAC only) and power it
    off. 409 while the host backs a cluster; 502 if the BMC can't do it (e.g. wipe on a non-Dell BMC)."""
    try:
        host = baremetal_service.reset(db, bmh_id, wipe=wipe)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except BMCError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not host:
        raise HTTPException(status_code=404, detail="Bare-metal host not found")
    return host
