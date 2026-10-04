"""Storage endpoints"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    StoragePool, StoragePoolCreate, Volume, VolumeCreate, ISOImage, ISODownload,
    CloudImage, CloudImageDownload, Task, TaskCreate,
)
from app.services.cloud_image_service import cloud_image_service
from app.services.storage_service import storage_service
from app.services.task_service import task_service

router = APIRouter()


# Storage Pools

@router.get("/pools", response_model=List[StoragePool])
def list_pools(db: Session = Depends(get_db)):
    """List all storage pools"""
    return storage_service.list_pools(db)


@router.get("/pools/{pool_id}", response_model=StoragePool)
def get_pool(pool_id: int, db: Session = Depends(get_db)):
    pool = storage_service.get_pool(db, pool_id)
    if not pool:
        raise HTTPException(status_code=404, detail="Storage pool not found")
    return pool


@router.post("/pools", response_model=StoragePool, status_code=201)
def create_pool(pool_data: StoragePoolCreate, db: Session = Depends(get_db)):
    """Create a directory storage pool"""
    try:
        return storage_service.create_pool(db, pool_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/pools/{pool_id}/start", response_model=StoragePool)
def start_pool(pool_id: int, db: Session = Depends(get_db)):
    pool = storage_service.set_pool_active(db, pool_id, True)
    if not pool:
        raise HTTPException(status_code=404, detail="Storage pool not found")
    return pool


@router.post("/pools/{pool_id}/stop", response_model=StoragePool)
def stop_pool(pool_id: int, db: Session = Depends(get_db)):
    pool = storage_service.set_pool_active(db, pool_id, False)
    if not pool:
        raise HTTPException(status_code=404, detail="Storage pool not found")
    return pool


@router.delete("/pools/{pool_id}")
def delete_pool(pool_id: int, db: Session = Depends(get_db)):
    """Remove a storage pool from libvirt (files on disk are kept)"""
    if not storage_service.delete_pool(db, pool_id):
        raise HTTPException(status_code=404, detail="Storage pool not found")
    return {"message": "Storage pool deleted successfully"}


# Volumes

@router.get("/volumes", response_model=List[Volume])
def list_all_volumes(db: Session = Depends(get_db)):
    """List volumes of all active pools"""
    return storage_service.list_volumes(db)


@router.get("/pools/{pool_id}/volumes", response_model=List[Volume])
def list_volumes(pool_id: int, db: Session = Depends(get_db)):
    return storage_service.list_volumes(db, pool_id)


@router.post("/pools/{pool_id}/volumes", response_model=Volume, status_code=201)
def create_volume(pool_id: int, vol_data: VolumeCreate, db: Session = Depends(get_db)):
    try:
        return storage_service.create_volume(db, pool_id, vol_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/volumes/{volume_id}")
def delete_volume(volume_id: int, db: Session = Depends(get_db)):
    if not storage_service.delete_volume(db, volume_id):
        raise HTTPException(status_code=404, detail="Volume not found")
    return {"message": "Volume deleted successfully"}


# ISO Images

@router.get("/isos", response_model=List[ISOImage])
def list_isos(db: Session = Depends(get_db)):
    """List ISO images (.iso volumes in active pools)"""
    return storage_service.list_isos(db)


@router.post("/isos/upload", status_code=201)
def upload_iso(
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
):
    """Upload an ISO into the default storage pool"""
    size = file.size
    if not size:
        file.file.seek(0, 2)
        size = file.file.tell()
        file.file.seek(0)
    path = storage_service.upload_iso(name or file.filename or "image.iso", size, file.file)
    return {"path": path, "size": size}


@router.post("/isos/download", response_model=Task, status_code=202)
def download_iso(data: ISODownload, db: Session = Depends(get_db)):
    """Download an ISO from a URL into the default pool as a background task"""
    name = storage_service.iso_name(data.name or data.url.rsplit("/", 1)[-1])
    return task_service.start(
        db,
        TaskCreate(name=f"Download {name}", type="iso_download", target_type="iso", target_name=name,
                   description=data.url),
        storage_service.download_iso,
        data.url,
        name,
    )


# Cloud Images

@router.get("/cloud-images", response_model=List[CloudImage])
def list_cloud_images(db: Session = Depends(get_db)):
    """List downloaded / downloading cloud images"""
    return cloud_image_service.list_images(db)


@router.get("/cloud-images/distributions")
def get_cloud_image_distributions():
    """Known distributions and their download URLs"""
    return cloud_image_service.get_distributions()


@router.post("/cloud-images", response_model=Task, status_code=202)
def download_cloud_image(data: CloudImageDownload, db: Session = Depends(get_db)):
    """Download a cloud image into the default pool as a background task"""
    try:
        return cloud_image_service.start_download(db, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/cloud-images/{image_id}")
def delete_cloud_image(image_id: int, db: Session = Depends(get_db)):
    """Delete a cloud image (VMs created from it are unaffected: they have their own copy)"""
    try:
        if not cloud_image_service.delete_image(db, image_id):
            raise HTTPException(status_code=404, detail="Cloud image not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": "Cloud image deleted successfully"}
