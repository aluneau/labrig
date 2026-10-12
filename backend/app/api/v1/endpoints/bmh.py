"""Bare-metal host endpoints (BMC-driven physical servers; services/bmc_service.py)"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    BareMetalHost, BareMetalHostCreate, BareMetalHostNic, BareMetalHostUpdate, BareMetalPower,
)
from app.services.bmc import BMCError
from app.services.bmc_service import bmc_service

router = APIRouter()


@router.get("", response_model=List[BareMetalHost])
def list_hosts(db: Session = Depends(get_db)):
    return bmc_service.list_hosts(db)


@router.post("", response_model=BareMetalHost, status_code=201)
def create_host(data: BareMetalHostCreate, db: Session = Depends(get_db)):
    """Register a bare-metal host by its BMC. The password is stored 0600 and never returned; the BMC is
    probed once (status 'error' with the reason if it is unreachable or the credentials are wrong)."""
    try:
        return bmc_service.create_host(db, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _host(db: Session, bmh_id: int):
    host = bmc_service.get_host(db, bmh_id)
    if not host:
        raise HTTPException(status_code=404, detail="Bare-metal host not found")
    return host


@router.get("/{bmh_id}", response_model=BareMetalHost)
def get_host(bmh_id: int, db: Session = Depends(get_db)):
    return _host(db, bmh_id)


@router.put("/{bmh_id}", response_model=BareMetalHost)
def update_host(bmh_id: int, data: BareMetalHostUpdate, db: Session = Depends(get_db)):
    host = bmc_service.update_host(db, bmh_id, data)
    if not host:
        raise HTTPException(status_code=404, detail="Bare-metal host not found")
    return host


@router.delete("/{bmh_id}")
def delete_host(bmh_id: int, db: Session = Depends(get_db)):
    try:
        if not bmc_service.delete_host(db, bmh_id):
            raise HTTPException(status_code=404, detail="Bare-metal host not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"message": "Bare-metal host removed"}


@router.post("/{bmh_id}/refresh", response_model=BareMetalHost)
def refresh_host(bmh_id: int, db: Session = Depends(get_db)):
    """Re-read inventory and power state from the BMC."""
    host = bmc_service.refresh(db, bmh_id)
    if not host:
        raise HTTPException(status_code=404, detail="Bare-metal host not found")
    return host


@router.get("/{bmh_id}/nics", response_model=List[BareMetalHostNic])
def host_nics(bmh_id: int, db: Session = Depends(get_db)):
    host = _host(db, bmh_id)
    try:
        return bmc_service.nics(db, host)
    except BMCError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{bmh_id}/power")
def power_host(bmh_id: int, data: BareMetalPower, db: Session = Depends(get_db)):
    """Power the host on, off (optionally graceful) or cycle it, through the BMC."""
    host = _host(db, bmh_id)
    try:
        state = bmc_service.power(db, host, data.action, graceful=data.graceful)
    except BMCError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"power_state": state}
