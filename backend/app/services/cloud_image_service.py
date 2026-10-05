"""Cloud images and cloud-init

Cloud images are downloaded as volumes of the default pool and tracked in the
cloud_images table. A VM created from one gets a full copy of the image as its
disk plus a NoCloud seed ISO ("<vm>-cidata.iso") generated from its settings.
"""
import io
import re
import uuid
from typing import List, Optional, Dict, Any

import pycdlib
import yaml
from sqlalchemy.orm import Session

from app.database import serialized
from app.libvirt_client import libvirt_client
from app.models import CloudImage, Task
from app.schemas import CloudImageDownload, TaskCreate

# Verified download URLs (x86_64). "url" may be overridden with a custom one.
DISTRIBUTIONS: List[Dict[str, Any]] = [
    {"name": "ubuntu", "label": "Ubuntu", "versions": {
        "26.04": "https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img",
        "24.04": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        "22.04": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
    }},
    {"name": "debian", "label": "Debian", "versions": {
        "13": "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2",
        "12": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2",
    }},
    {"name": "almalinux", "label": "AlmaLinux", "versions": {
        "10": "https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2",
        "9": "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
    }},
    {"name": "rocky", "label": "Rocky Linux", "versions": {
        "9": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
    }},
    {"name": "centos", "label": "CentOS Stream", "versions": {
        "10-stream": "https://cloud.centos.org/centos/10-stream/x86_64/images/CentOS-Stream-GenericCloud-10-latest.x86_64.qcow2",
        "9-stream": "https://cloud.centos.org/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2",
    }},
]


class CloudImageService:

    def get_distributions(self) -> List[Dict[str, Any]]:
        return [
            {"name": d["name"], "label": d["label"],
             "versions": [{"version": v, "url": u} for v, u in d["versions"].items()]}
            for d in DISTRIBUTIONS
        ]

    @serialized
    def list_images(self, db: Session) -> List[CloudImage]:
        """List images; flag ready ones whose volume vanished, and adopt orphan cloudimg-* volumes"""
        from app.services.storage_service import storage_service

        volumes = {v["path"]: v for v in storage_service.list_volumes(db)}
        images = db.query(CloudImage).all()

        for image in images:
            if image.status in ("ready", "missing"):
                image.status = "ready" if libvirt_client.volume_exists(image.path) else "missing"

        known = {i.path for i in images} | {i.name for i in images}
        for path, vol in volumes.items():
            match = re.fullmatch(r"cloudimg-([a-z0-9]+)-(.+)\.qcow2", vol["name"])
            if match and path not in known and vol["name"] not in known:
                db.add(CloudImage(name=vol["name"], distribution=match[1], version=match[2], arch="x86_64",
                                  url="", path=path, size=vol["allocation"], status="ready", download_progress=100))

        db.commit()
        return db.query(CloudImage).order_by(CloudImage.created_at.desc()).all()

    def get_image(self, db: Session, image_id: int) -> Optional[CloudImage]:
        return db.query(CloudImage).filter(CloudImage.id == image_id).first()

    def start_download(self, db: Session, data: CloudImageDownload) -> Task:
        from app.services.task_service import task_service

        url = data.url
        if not url:
            dist = next((d for d in DISTRIBUTIONS if d["name"] == data.distribution), None)
            url = dist and dist["versions"].get(data.version)
            if not url:
                raise ValueError(f"Unknown distribution/version {data.distribution} {data.version}; give a url")

        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", f"{data.distribution}-{data.version}").lower()
        volume_name = f"cloudimg-{slug}.qcow2"
        if db.query(CloudImage).filter(CloudImage.name == volume_name,
                                       CloudImage.status.in_(["downloading", "ready"])).first():
            raise ValueError(f"Cloud image {data.distribution} {data.version} is already downloaded or downloading")

        image = CloudImage(
            name=volume_name,
            distribution=data.distribution,
            version=data.version,
            arch="x86_64",
            url=url,
            description=data.description,
            status="downloading",
        )
        db.add(image)
        db.commit()

        return task_service.start(
            db,
            TaskCreate(name=f"Download cloud image {data.distribution} {data.version}", type="cloud_image_download",
                       target_type="cloud_image", target_id=image.id, target_name=volume_name, description=url),
            self._download, image.id,
        )

    def _download(self, db: Session, task: Task, image_id: int) -> Dict[str, Any]:
        from app.services.storage_service import storage_service

        image = self.get_image(db, image_id)

        def on_progress(pct: int):
            image.download_progress = pct
            db.commit()

        try:
            result = storage_service.download_to_pool(db, task, image.url, image.name, on_progress)
        except Exception:
            image.status = "error"
            db.commit()
            raise
        image.status = "ready"
        image.download_progress = 100
        image.path = result["path"]
        image.size = result["size"]
        db.commit()
        return result

    def delete_image(self, db: Session, image_id: int) -> bool:
        image = self.get_image(db, image_id)
        if not image:
            return False
        if image.status == "downloading":
            raise ValueError("Image is still downloading; cancel the task first")
        if image.path:
            libvirt_client.delete_volume_by_path(image.path)
        db.delete(image)
        db.commit()
        return True

    # cloud-init

    def build_user_data(self, hostname: str, username: Optional[str], password: Optional[str],
                        ssh_keys: List[str], custom: Optional[str], keyboard: Optional[str] = None,
                        fqdn: Optional[str] = None) -> str:
        if custom and custom.strip():
            return custom
        config = self.build_cloud_config(hostname, username, password, ssh_keys, keyboard, fqdn)
        return "#cloud-config\n" + yaml.safe_dump(config, sort_keys=False)

    def build_cloud_config(self, hostname: str, username: Optional[str], password: Optional[str],
                           ssh_keys: List[str], keyboard: Optional[str] = None,
                           fqdn: Optional[str] = None) -> Dict[str, Any]:
        """#cloud-config as a dict (users, keyboard, guest agent), for callers that extend it"""
        config: Dict[str, Any] = {
            "hostname": hostname,
            "fqdn": fqdn or hostname,
            "manage_etc_hosts": True,
            "package_update": True,
            "packages": ["qemu-guest-agent"],
            "runcmd": [["systemctl", "enable", "--now", "qemu-guest-agent"]],
        }
        if keyboard:
            # The VNC console types physical keys, so the text console needs this layout. Not cloud-init's
            # keyboard module: on Debian it restarts console-setup before packages are installed (cloud-init
            # ends in "error"), and EL's cloud.cfg doesn't run it at all.
            # Debian/Ubuntu: console-setup + XKBLAYOUT + setupcon. EL: the UI's XKB names all exist as
            # console keymaps (set-x11-keymap fails on EL9 without xkeyboard-config, so it's the fallback).
            config["runcmd"].append(
                "if command -v apt-get >/dev/null; then"
                " DEBIAN_FRONTEND=noninteractive apt-get install -y console-setup"
                f" && sed -i 's/^XKBLAYOUT=.*/XKBLAYOUT=\"{keyboard}\"/' /etc/default/keyboard"
                " && setupcon --force --save;"
                f" else {{ localectl set-keymap {keyboard} || localectl set-x11-keymap {keyboard}; }}"
                " && systemctl restart systemd-vconsole-setup; fi || true")
        if username:
            user: Dict[str, Any] = {
                "name": username,
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "shell": "/bin/bash",
                "lock_passwd": not password,
            }
            if password:
                user["plain_text_passwd"] = password
            if ssh_keys:
                user["ssh_authorized_keys"] = ssh_keys
            config["users"] = ["default", user]
            config["ssh_pwauth"] = bool(password)
        elif ssh_keys:
            config["ssh_authorized_keys"] = ssh_keys
        return config

    def build_seed_iso(self, hostname: str, user_data: str, network_config: Optional[str] = None) -> bytes:
        """NoCloud seed: ISO9660 volume labelled 'cidata' with user-data, meta-data
        and optionally network-config (cloud-init network config v2)"""
        meta_data = f"instance-id: {hostname}-{uuid.uuid4().hex[:8]}\nlocal-hostname: {hostname}\n"
        iso = pycdlib.PyCdlib()
        iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="cidata")
        files = [("USERDATA.;1", "user-data", user_data), ("METADATA.;1", "meta-data", meta_data)]
        if network_config:
            files.append(("NETWORK.;1", "network-config", network_config))
        for iso_name, name, content in files:
            data = content.encode()
            iso.add_fp(io.BytesIO(data), len(data), f"/{iso_name}", rr_name=name, joliet_path=f"/{name}")
        out = io.BytesIO()
        iso.write_fp(out)
        iso.close()
        return out.getvalue()


cloud_image_service = CloudImageService()
