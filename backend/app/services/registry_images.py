"""Images added by hand to a group's mirror registry (docs/disconnected.md "Your own images")

    copy      POST /groups/{id}/registry/images: `skopeo copy --all` ON THE ROUTER (it has internet), detached
              unit vmm-copy-<id> polled through the guest agent. Source credentials (optional) + the OpenShift
              pull secret + the registry's credentials go into /run/vmm-copy-<id>/auth.json (tmpfs, 0600),
              deleted when skopeo exits; never stored.
    upload    POST /groups/{id}/registry/upload: the browser streams a `podman save` / `docker save` / OCI archive;
              the app spools it to DATA_DIR/groups/<group>/uploads/ (disk, not memory) and pushes it from the host
              to <uplink_ip>:<port> (registry_client: plain registry v2 API, no tool needed on the host). The file
              is deleted afterwards.
    list      GET /groups/{id}/registry/images: catalog + tags through the registry API (from the host)
    delete    DELETE /groups/{id}/registry/images?ref=repo:tag
    push      podman push from the host / a WireGuard laptop with GET /groups/{id}/registry/credentials

Images added through the app are recorded in DATA_DIR/groups/<group>/images.json ("added" in the list).
"""
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import libvirt
from sqlalchemy.orm import Session

from app.models import Group, Task
from app.schemas import TaskCreate
from app.schemas.registry import ImageCopyRequest, RegistryCredentials, RegistryImage, RegistryImages
from app.services import registry_router as rr
from app.services import registry_service as rs
from app.services.registry_client import ArchivePusher, RegistryClient, RegistryError, split_ref
from app.services.task_service import task_service

logger = logging.getLogger(__name__)

COPY_TIMEOUT = 2 * 60 * 60
MAX_REPOS = 500


def _ready_spec(group: Group):
    spec = rs._spec(group)
    if not spec.router.registry.enabled:
        raise ValueError("The mirror registry is not enabled on this group")
    if not spec.router.uplink_ip:
        raise ValueError("The router's uplink address is not known yet (start the group)")
    return spec


def client(group: Group, timeout: float = 60) -> RegistryClient:
    """Registry API client from the host (through the router's uplink address)"""
    spec = _ready_spec(group)
    user, password = rs._credentials(group.name)
    return RegistryClient(rs._hosts(spec)[1], spec.router.registry.ca_pem, user, password, timeout=timeout)


def credentials(group: Group) -> RegistryCredentials:
    spec = _ready_spec(group)
    user, password = rs._credentials(group.name)
    hosts = rs._hosts(spec)
    return RegistryCredentials(username=user, password=password, registry=hosts[0], uplink_registry=hosts[1] or None)


# ------------------------------------------------------------------ records of added images

def _added_path(name: str) -> Path:
    return rs._group_dir(name) / "images.json"


DELETED = "_deleted"  # refs deleted through the app: Quay's tags/list keeps showing them for a while


def _added(name: str) -> Dict[str, Any]:
    path = _added_path(name)
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        return {}


def _save_added(name: str, added: Dict[str, Any]) -> None:
    _added_path(name).write_text(json.dumps(added, indent=1))


def _record_added(name: str, refs: List[str], source: str) -> None:
    added = _added(name)
    deleted = set(added.get(DELETED, []))
    for ref in refs:
        added[ref] = source
        deleted.discard(ref)
    added[DELETED] = sorted(deleted)
    _save_added(name, added)


# ------------------------------------------------------------------ list / delete

def list_images(group: Group) -> RegistryImages:
    spec = _ready_spec(group)
    hosts = rs._hosts(spec)
    c = client(group, timeout=20)
    try:
        repos = c.catalog(MAX_REPOS + 1)
    except (RegistryError, OSError) as e:
        raise RuntimeError(f"Could not list the registry: {e}")
    added = _added(group.name)
    deleted = set(added.get(DELETED, []))
    images = []
    for repo in sorted(repos[:MAX_REPOS]):
        try:
            # cosign signatures / attestations oc-mirror copies along (sha256-<digest>.sig) are not images to pull
            tags = sorted(t for t in c.tags(repo) if not (t.startswith("sha256-") and t.endswith((".sig", ".att", ".sbom"))))
        except (RegistryError, OSError):
            tags = []
        gone = []
        for t in tags:
            if f"{repo}:{t}" in deleted:
                try:
                    c.digest(repo, t)  # pushed again outside the app
                except (RegistryError, OSError):
                    gone.append(t)
        tags = [t for t in tags if t not in gone]
        if not tags:
            continue
        sources = {t: added[f"{repo}:{t}"] for t in tags if f"{repo}:{t}" in added}
        images.append(RegistryImage(repository=repo, tags=tags, added=bool(sources), sources=sources))
    return RegistryImages(registry=hosts[0], uplink_registry=hosts[1] or None, images=images,
                          truncated=len(repos) > MAX_REPOS)


def delete_image(group: Group, ref: str) -> None:
    repo, tag = split_ref(ref)
    try:
        client(group).delete_tag(repo, tag)
    except RegistryError as e:
        raise RuntimeError(str(e))
    added = _added(group.name)
    added.pop(f"{repo}:{tag}", None)
    added[DELETED] = sorted(set(added.get(DELETED, [])) | {f"{repo}:{tag}"})
    _save_added(group.name, added)


# ------------------------------------------------------------------ copy (skopeo on the router)

def _dest(spec, req: ImageCopyRequest) -> Tuple[str, str]:
    src = req.source.strip()
    if "@" in src:  # by digest: tag it with the source tag if any, else sha256-<12 hex>
        name, digest = src.split("@", 1)
        repo, tag = split_ref(name)
        if ":" not in name.rsplit("/", 1)[-1]:
            tag = "sha256-" + digest.split(":")[-1][:12]
    else:
        repo, tag = split_ref(src)
    return req.dest_repo or repo, req.dest_tag or tag


def _source_registry(source: str) -> str:
    first = source.split("/", 1)[0]
    return first if ("." in first or ":" in first or first == "localhost") and "/" in source else "docker.io"


def start_copy(db: Session, group: Group, req: ImageCopyRequest) -> Task:
    spec = _ready_spec(group)
    repo, tag = _dest(spec, req)
    return task_service.start(
        db, TaskCreate(name=f"Copy {req.source} into {group.name} registry", type="registry_copy", target_type="registry",
                       target_id=group.id, target_name=group.name, description=f"-> {rs._hosts(spec)[0]}/{repo}:{tag}"),
        _copy_task, group.id, req.model_dump(), repo, tag)


def _copy_task(db: Session, task: Task, group_id: int, req_dict: Dict[str, Any], repo: str, tag: str) -> Dict[str, Any]:
    req = ImageCopyRequest.model_validate(req_dict)
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise ValueError("The group no longer exists")
    progress = rs._Progress(db, task)
    rs.ensure_ready(db, group, task, progress.span(0, 10))
    spec = rs._spec(group)
    rtr = rs._rtr(group)
    dest = f"{rs._hosts(spec)[0]}/{repo}:{tag}"
    copy_id = uuid.uuid4().hex[:12]
    run_dir = f"{rr.DATA}/copies/{copy_id}"
    auths = dict(rs._pull_auths())
    if req.username:
        import base64
        auths[_source_registry(req.source)] = {"auth": base64.b64encode(f"{req.username}:{req.password or ''}".encode()).decode()}
    auths.update(rs._auths(spec, group.name)["auths"])
    from app.services.group_service import group_service
    group_service._agent_put(rtr, f"{run_dir}/job", f"{req.source.strip()}\n{dest}\n".encode())
    result = rs._exec(rtr, f"umask 077 && mkdir -p /run/vmm-copy-{copy_id} && cat > /run/vmm-copy-{copy_id}/auth.json",
                      input_data=json.dumps({"auths": auths}).encode())
    if result["exitcode"] != 0:
        raise RuntimeError(f"Could not write the copy credentials on the router: {result['stderr'].strip()}")
    result = rs._exec(rtr, f"systemd-run --unit=vmm-copy-{copy_id} --collect /bin/bash {rr.SCRIPT} copy {copy_id}")
    if result["exitcode"] != 0:
        rs._exec(rtr, f"rm -rf /run/vmm-copy-{copy_id}")
        raise RuntimeError(f"Could not start skopeo on the router: {result['stderr'].strip()}")
    progress.span(10, 99)
    deadline = time.monotonic() + COPY_TIMEOUT
    blobs = 0
    while True:
        if task_service.is_cancelled(task.id):
            rs._exec(rtr, f"systemctl stop vmm-copy-{copy_id} 2>/dev/null; true")
            raise RuntimeError("Cancelled")
        try:
            out = rs._exec(rtr, f"systemctl is-active -q vmm-copy-{copy_id} && echo ACTIVE; "
                                f"echo \"RC=$(cat {run_dir}/rc 2>/dev/null)\"; tail -n 400 {run_dir}/log 2>/dev/null",
                           timeout=30)["stdout"]
        except (libvirt.libvirtError, TimeoutError):
            time.sleep(rs.POLL)
            continue
        lines = [ln for ln in out.splitlines() if ln.strip()]
        blobs = sum(1 for ln in lines if ln.startswith("Copying blob"))
        last = next((ln for ln in reversed(lines) if not ln.startswith(("RC=", "ACTIVE"))), "")
        progress(min(blobs / 20, 0.9), f"skopeo: {last[:150]}")
        if not out.startswith("ACTIVE"):
            rc = next((ln[3:] for ln in lines if ln.startswith("RC=")), "") or "1"
            break
        if time.monotonic() > deadline:
            raise TimeoutError("skopeo copy still running after 2 h")
        time.sleep(3)
    rs._exec(rtr, f"rm -rf /run/vmm-copy-{copy_id}")
    if rc != "0":
        tail = "\n".join(lines[-8:])
        raise RuntimeError(f"skopeo copy failed ({rc}): {tail[-1500:]}")
    rs._exec(rtr, f"rm -rf {run_dir}")
    _record_added(group.name, [f"{repo}:{tag}"], f"copy:{req.source.strip()}")
    progress(1.0, f"Copied to {dest}")
    return {"image": dest, "uplink_image": f"{rs._hosts(spec)[1]}/{repo}:{tag}", "blobs": blobs}


# ------------------------------------------------------------------ upload (archive pushed from the host)

def upload_dir(group: Group) -> Path:
    path = rs._group_dir(group.name) / "uploads"
    path.mkdir(exist_ok=True)
    os.chmod(path, 0o700)
    return path


def start_push_archive(db: Session, group: Group, path: Path, filename: str, repo: Optional[str],
                       tag: Optional[str]) -> Task:
    _ready_spec(group)
    return task_service.start(
        db, TaskCreate(name=f"Push {filename} into {group.name} registry", type="registry_upload", target_type="registry",
                       target_id=group.id, target_name=group.name,
                       description=f"{path.stat().st_size // 1_000_000} MB archive"),
        _push_task, group.id, str(path), filename, repo, tag)


def _push_task(db: Session, task: Task, group_id: int, path: str, filename: str, repo: Optional[str],
               tag: Optional[str]) -> Dict[str, Any]:
    try:
        group = db.query(Group).filter(Group.id == group_id).first()
        if group is None:
            raise ValueError("The group no longer exists")
        progress = rs._Progress(db, task)
        rs.ensure_ready(db, group, task, progress.span(0, 5))
        progress.span(5, 99)
        last = [0.0]

        def report(done: int, total: int, name: str) -> None:
            if time.monotonic() - last[0] > 2:
                last[0] = time.monotonic()
                progress(done / max(total, 1), f"pushing {done // 1_000_000} / {total // 1_000_000} MB")

        pusher = ArchivePusher(client(group, timeout=300), path, report, lambda: task_service.is_cancelled(task.id))
        try:
            refs = pusher.push(repo, tag)
        finally:
            pusher.close()
        _record_added(group.name, refs, f"upload:{filename}")
        spec = rs._spec(group)
        hosts = rs._hosts(spec)
        progress(1.0, f"Pushed {', '.join(refs)}")
        return {"images": [f"{hosts[0]}/{r}" for r in refs], "uplink_images": [f"{hosts[1]}/{r}" for r in refs]}
    except RegistryError as e:
        raise RuntimeError(str(e))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def new_upload_path(group: Group) -> Path:
    return upload_dir(group) / f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{hashlib.sha1(os.urandom(8)).hexdigest()[:8]}.tar"
