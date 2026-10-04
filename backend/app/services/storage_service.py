"""Storage Service

libvirt is the source of truth; pools and volumes are mirrored into the
database so the API can expose stable integer IDs.
"""
import logging
import os
import re
import tempfile
from typing import List, Optional, Dict, Any, BinaryIO, Callable
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.database import serialized
from app.libvirt_client import libvirt_client
from app.models import StoragePool, Volume, Task
from app.schemas import StoragePoolCreate, VolumeCreate

logger = logging.getLogger(__name__)


class StorageService:
    """Storage management service"""

    # Pools

    @serialized
    def sync_pools(self, db: Session) -> None:
        libvirt_pools = {p["uuid"]: p for p in libvirt_client.list_storage_pools()}

        for pool in db.query(StoragePool).all():
            if pool.uuid not in libvirt_pools:
                db.query(Volume).filter(Volume.pool_id == pool.id).delete()
                db.delete(pool)
        db.flush()

        for lv_pool in libvirt_pools.values():
            pool = db.query(StoragePool).filter(StoragePool.uuid == lv_pool["uuid"]).first()
            if pool is None:
                pool = StoragePool(uuid=lv_pool["uuid"])
                db.add(pool)
            for field in ("name", "type", "path", "state", "capacity", "allocation", "available", "autostart"):
                setattr(pool, field, lv_pool[field])
            pool.xml_config = lv_pool["xml"]

        db.commit()

    def list_pools(self, db: Session) -> List[StoragePool]:
        self.sync_pools(db)
        return db.query(StoragePool).order_by(StoragePool.name).all()

    def get_pool(self, db: Session, pool_id: int) -> Optional[StoragePool]:
        return db.query(StoragePool).filter(StoragePool.id == pool_id).first()

    def create_pool(self, db: Session, pool_data: StoragePoolCreate) -> StoragePool:
        if db.query(StoragePool).filter(StoragePool.name == pool_data.name).first():
            raise ValueError(f"Storage pool '{pool_data.name}' already exists")
        pool_uuid = libvirt_client.create_storage_pool(
            name=pool_data.name,
            path=pool_data.path,
            pool_type=pool_data.type,
            autostart=pool_data.autostart,
        )
        self.sync_pools(db)
        return db.query(StoragePool).filter(StoragePool.uuid == pool_uuid).first()

    def set_pool_active(self, db: Session, pool_id: int, active: bool) -> Optional[StoragePool]:
        pool = self.get_pool(db, pool_id)
        if not pool:
            return None
        if active:
            libvirt_client.start_storage_pool(pool.name)
        else:
            libvirt_client.stop_storage_pool(pool.name)
        self.sync_pools(db)
        return self.get_pool(db, pool_id)

    def delete_pool(self, db: Session, pool_id: int) -> bool:
        """Undefine the pool in libvirt; files on disk are kept"""
        pool = self.get_pool(db, pool_id)
        if not pool:
            return False
        libvirt_client.delete_storage_pool(pool.name)
        db.query(Volume).filter(Volume.pool_id == pool.id).delete()
        db.delete(pool)
        db.commit()
        return True

    # Volumes

    def _sync_volumes(self, db: Session, pool: StoragePool) -> None:
        lv_volumes = {v["name"]: v for v in libvirt_client.list_volumes(pool.name)}

        for vol in db.query(Volume).filter(Volume.pool_id == pool.id).all():
            if vol.name not in lv_volumes:
                db.delete(vol)

        for lv_vol in lv_volumes.values():
            rows = db.query(Volume).filter(Volume.pool_id == pool.id, Volume.name == lv_vol["name"]) \
                .order_by(Volume.id).all()
            for duplicate in rows[1:]:
                db.delete(duplicate)
            vol = rows[0] if rows else None
            if vol is None:
                vol = Volume(pool_id=pool.id, name=lv_vol["name"])
                db.add(vol)
            vol.type = lv_vol["type"]
            vol.format = lv_vol["format"]
            vol.capacity = lv_vol["capacity"]
            vol.allocation = lv_vol["allocation"]
            vol.path = lv_vol["path"]

    def _volume_dicts(self, db: Session, query) -> List[Dict[str, Any]]:
        pool_names = {p.id: p.name for p in db.query(StoragePool).all()}
        return [
            {**{c.name: getattr(v, c.name) for c in Volume.__table__.columns}, "pool_name": pool_names.get(v.pool_id)}
            for v in query.order_by(Volume.name).all()
        ]

    @serialized
    def list_volumes(self, db: Session, pool_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """List volumes of one pool, or of all pools"""
        self.sync_pools(db)
        pools = db.query(StoragePool)
        if pool_id is not None:
            pools = pools.filter(StoragePool.id == pool_id)
        for pool in pools.all():
            if pool.state == "active":
                self._sync_volumes(db, pool)
        db.commit()

        query = db.query(Volume)
        if pool_id is not None:
            query = query.filter(Volume.pool_id == pool_id)
        return self._volume_dicts(db, query)

    def create_volume(self, db: Session, pool_id: int, vol_data: VolumeCreate) -> Dict[str, Any]:
        pool = self.get_pool(db, pool_id)
        if not pool:
            raise ValueError("Pool not found")
        libvirt_client.create_volume(pool.name, vol_data.name, vol_data.capacity, vol_data.format)
        volumes = self.list_volumes(db, pool_id)
        return next(v for v in volumes if v["name"] == vol_data.name)

    def delete_volume(self, db: Session, volume_id: int) -> bool:
        vol = db.query(Volume).filter(Volume.id == volume_id).first()
        if not vol:
            return False
        pool = self.get_pool(db, vol.pool_id)
        if pool:
            libvirt_client.delete_volume(pool.name, vol.name)
        db.delete(vol)
        db.commit()
        return True

    # ISO images (stored as volumes so QEMU can read them)

    def list_isos(self, db: Session) -> List[Dict[str, Any]]:
        return [
            {"name": v["name"], "pool_name": v["pool_name"], "path": v["path"], "size": v["capacity"]}
            for v in self.list_volumes(db)
            if v["name"].lower().endswith(".iso") and not v["name"].endswith("-cidata.iso") and v["path"]
        ]

    @staticmethod
    def iso_name(raw: str) -> str:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(raw)).lstrip("._-") or "image"
        return name if name.lower().endswith(".iso") else f"{name}.iso"

    def _default_pool_name(self) -> str:
        return libvirt_client.ensure_pool(settings.DEFAULT_POOL_NAME, settings.DEFAULT_POOL_PATH).name()

    def upload_iso(self, name: str, size: int, fileobj: BinaryIO) -> str:
        return libvirt_client.upload_volume(self._default_pool_name(), self.iso_name(name), size, fileobj)

    def download_iso(self, db: Session, task: Task, url: str, name: Optional[str]) -> Dict[str, Any]:
        """Task body: stream an ISO from a URL into the default pool"""
        return self.download_to_pool(db, task, url, self.iso_name(name or urlparse(url).path))

    def download_to_pool(self, db: Session, task: Task, url: str, name: str,
                         on_progress: Optional[Callable[[int], None]] = None) -> Dict[str, Any]:
        """Download a URL into a new volume of the default pool, reporting task progress.

        Streams straight into libvirt when the server announces the size; otherwise
        (no Content-Length) the file is first spooled to DATA_DIR, then uploaded.
        """
        from app.services.task_service import task_service

        pool_name = self._default_pool_name()
        last_pct = -1

        def report(done: int, total: int) -> None:
            nonlocal last_pct
            if task_service.is_cancelled(task.id):
                raise RuntimeError("Cancelled")
            pct = min(done * 100 // total, 99) if total else 0
            if pct != last_pct:
                last_pct = pct
                task_service.update_progress(db, task.id, pct)
                if on_progress:
                    on_progress(pct)

        # identity: the raw file, so Content-Length (when sent) is the real size
        headers = {"Accept-Encoding": "identity"}
        with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=60) as resp:
            resp.raise_for_status()
            size = int(resp.headers.get("content-length") or 0)
            chunks = resp.iter_raw(1024 * 1024)

            if size:
                received = 0

                class Reader:
                    def read(self, _n):
                        nonlocal received
                        report(received, size)
                        chunk = next(chunks, b"")
                        received += len(chunk)
                        return chunk

                path = libvirt_client.upload_volume(pool_name, name, size, Reader())
            else:
                spool_dir = settings.DATA_DIR / "downloads"
                spool_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryFile(dir=spool_dir) as spool:
                    for chunk in chunks:
                        report(0, 0)
                        spool.write(chunk)
                    size = spool.tell()
                    spool.seek(0)
                    path = libvirt_client.upload_volume(pool_name, name, size, spool)

        libvirt_client.refresh_pool(pool_name)
        return {"path": path, "size": size}


storage_service = StorageService()
