"""Mirror registry on the group router (docs/disconnected.md)

`spec.router.registry.enabled` makes the group router the lab's "bastion": Red Hat's mirror-registry
(Quay on podman) on an extra router disk (serial vmm-registry), filled by oc-mirror v2 running on the
router itself (it keeps its internet access while the lab's egress can be blocked, router.egress).

    router RAM / vCPUs  registry.memory_mb / vcpus (saved config; the setup restarts the router once)
    names               registry.<domain>:<port> (router dnsmasq + router /etc/hosts), <uplink_ip>:<port>
    credentials         DATA_DIR/groups/<group>/registry-auth.json (0600), made here, pushed to the router
                        (/etc/vmm-registry/credentials 0600); never in the spec, the DB or API responses
    CA                  made on the router, read back into spec.router.registry.ca_pem
    mirror runs         one oc-mirror workspace per request (hash) in /var/lib/vmm-registry/runs/<id>,
                        run detached (systemd-run unit vmm-mirror-<id>) and polled through the guest agent:
                        an app restart or a guest-exec timeout doesn't stop it; re-running a finished
                        request only reads its results back. Records: DATA_DIR/groups/<group>/mirrors.json
    pull secret         the OpenShift pull secret (openshift_service) is merged with the registry auth into
                        /run/vmm-mirror-<id>/auth.json (tmpfs, 0600) for the run only, deleted by the unit
                        when oc-mirror exits. Never logged nor returned.
"""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import string
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import libvirt
import yaml
from sqlalchemy.orm import Session

from app.config import settings
from app.libvirt_client import libvirt_client
from app.models import Group, Task
from app.schemas import TaskCreate
from app.schemas.group import GroupSpec, REGISTRY_DISK_SERIAL
from app.schemas.registry import (
    ImageDigestSource, MirrorRecord, MirrorRequest, MirrorResult, RegistryStatus,
)
from app.services import registry_router as rr
from app.services.task_service import task_service

logger = logging.getLogger(__name__)

REGISTRY_USER = "vmm"
SETUP_TIMEOUT = 60 * 60       # 1.3 GB download + Quay install
MIRROR_TIMEOUT = 8 * 60 * 60  # a release + a few operators on a slow line
POLL = 10

_locks: Dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _lock(group: str, kind: str) -> threading.Lock:
    with _locks_lock:
        return _locks.setdefault(f"{group}:{kind}", threading.Lock())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _rtr(group: Group) -> str:
    from app.services.group_service import router_vm_name
    return group.router_vm_name or router_vm_name(group.name)


def _spec(group: Group) -> GroupSpec:
    return GroupSpec.model_validate(group.spec)


# ---------------------------------------------------------------- host-side files

def _group_dir(name: str) -> Path:
    path = Path(settings.DATA_DIR) / "groups" / name
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _credentials(name: str) -> Tuple[str, str]:
    """(user, password) of the group's registry, made on first use"""
    path = _group_dir(name) / "registry-auth.json"
    if path.exists():
        data = json.loads(path.read_text())
        return data["username"], data["password"]
    password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"username": REGISTRY_USER, "password": password}, f)
    return REGISTRY_USER, password


def _auths(spec: GroupSpec, name: str) -> Dict[str, Any]:
    user, password = _credentials(name)
    auth = {"auth": base64.b64encode(f"{user}:{password}".encode()).decode()}
    return {"auths": {host: dict(auth) for host in _hosts(spec) if host}}


def _hosts(spec: GroupSpec) -> List[str]:
    reg = spec.router.registry
    return [f"registry.{spec.domain}:{reg.port}",
            f"{spec.router.uplink_ip}:{reg.port}" if spec.router.uplink_ip else ""]


def _records_path(name: str) -> Path:
    return _group_dir(name) / "mirrors.json"


def _load_records(name: str) -> Dict[str, Dict[str, Any]]:
    path = _records_path(name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except ValueError:
        return {}


def _save_record(name: str, record: MirrorRecord) -> None:
    records = _load_records(name)
    records[record.id] = record.model_dump(mode="json")
    tmp = _records_path(name).with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=1))
    tmp.replace(_records_path(name))


def forget(name: str) -> None:
    """Group deleted: drop its credentials and mirror records"""
    path = Path(settings.DATA_DIR) / "groups" / name
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------- router VM resources

def ensure_router_disk(spec: GroupSpec) -> Dict[str, Any]:
    """Registry disk attached (thin qcow2 in the default pool, serial vmm-registry, hot-plugged when the router
    runs), grown to disk_gb; saved-config RAM / vCPUs = what the spec needs (applies at the next cold start).
    Returns {"disk_added", "resources_changed", "restart_required"}."""
    from app.services.group_service import router_vm_name
    rtr = router_vm_name(spec.name)
    reg = spec.router.registry
    result = {"disk_added": False, "resources_changed": False, "restart_required": False}
    if libvirt_client.get_vm(rtr) is None:
        return result
    result["resources_changed"] = libvirt_client.set_config_resources(
        rtr, spec.router.effective_memory(), spec.router.effective_vcpu())
    if not reg.enabled:
        return result
    from app.domain_xml import data_disk_xml
    disk = libvirt_client.disk_with_serial(rtr, REGISTRY_DISK_SERIAL)
    size = reg.disk_gb * 1024 ** 3
    if disk is None:
        pool = libvirt_client.ensure_pool(settings.DEFAULT_POOL_NAME, settings.DEFAULT_POOL_PATH).name()
        name = f"{rtr}-registry.qcow2"
        if libvirt_client.volume_exists(str(Path(settings.DEFAULT_POOL_PATH) / name)):
            name = libvirt_client.free_volume_name(pool, f"{rtr}-registry", "qcow2")  # left by a group kept on disk
        path = libvirt_client.create_volume(pool, name, size, "qcow2")
        target = libvirt_client.next_disk_target(rtr, "vd")
        attached = libvirt_client.attach_disk(rtr, data_disk_xml(path, target, "virtio", "qcow2",
                                                                 serial=REGISTRY_DISK_SERIAL))
        logger.info(f"Registry disk {path} attached to {rtr} as {target} (pending={attached['pending']})")
        result["disk_added"] = True
        result["restart_required"] = attached["pending"]
    elif disk["path"] and libvirt_client.volume_capacity(disk["path"]) < size:
        libvirt_client.resize_disk(rtr, disk["target"], size)
    live = libvirt_client.get_vm(rtr) or {}
    if live.get("state") == "running" and live.get("memory", 0) < spec.router.effective_memory() * 1024 * 0.95:
        result["restart_required"] = True
    return result


# ---------------------------------------------------------------- router script

def _exec(rtr: str, script: str, timeout: float = 60, input_data: Optional[bytes] = None) -> Dict[str, Any]:
    return libvirt_client.agent_exec(rtr, "/bin/bash", ["-c", script], input_data=input_data, timeout=timeout)


def _router_status(rtr: str) -> Dict[str, Any]:
    out = _exec(rtr, f"test -f {rr.SCRIPT} && bash {rr.SCRIPT} status; "
                     f"for d in {rr.MIRROR_RUNS}/*/; do [ -d \"$d\" ] || continue; "
                     "echo \"run=$(basename $d):$(cat $d/rc 2>/dev/null)\"; done", timeout=30)["stdout"]
    status: Dict[str, Any] = {"mirror_active": [], "runs": {}}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == "mirror_active":
            status["mirror_active"].append(re.sub(r"\.service$", "", value))
        elif key == "run":
            run_id, _, rc = value.partition(":")
            status["runs"][run_id] = rc
        else:
            status[key] = value
    if status.get("ca"):
        try:
            status["ca"] = base64.b64decode(status["ca"]).decode()
        except (ValueError, UnicodeDecodeError):
            status["ca"] = None
    return status


def _push_credentials(rtr: str, name: str) -> None:
    user, password = _credentials(name)
    data = f"VMM_REG_USER={user}\nVMM_REG_PASSWORD={password}\n".encode()
    result = _exec(rtr, f"umask 077 && mkdir -p $(dirname {rr.CREDENTIALS}) && cat > {rr.CREDENTIALS}",
                   input_data=data)
    if result["exitcode"] != 0:
        raise RuntimeError(f"Could not write the registry credentials on the router: {result['stderr'].strip()}")


def _router_running(rtr: str) -> bool:
    return (libvirt_client.get_vm(rtr) or {}).get("state") == "running"


# ---------------------------------------------------------------- status

def status(db: Session, group: Group) -> RegistryStatus:
    spec = _spec(group)
    reg = spec.router.registry
    rtr = _rtr(group)
    hosts = _hosts(spec)
    out = RegistryStatus(enabled=reg.enabled, hostname=reg.hostname, port=reg.port,
                         url=hosts[0] if reg.enabled else None, uplink_url=(hosts[1] or None) if reg.enabled else None,
                         ca_pem=reg.ca_pem, memory_mb=spec.router.effective_memory(), vcpus=spec.router.effective_vcpu(),
                         egress=spec.router.egress.mode, egress_allow=spec.router.egress.allow,
                         setup_task_id=_running_task(db, group, "registry_setup"),
                         mirror_task_id=_running_task(db, group, "registry_mirror"))
    records = {k: MirrorRecord.model_validate(v) for k, v in _load_records(group.name).items()}
    live = libvirt_client.get_vm(rtr) or {}
    if live.get("state") == "running":
        out.router_memory_mb = int(live.get("memory", 0) / 1024)
    router: Dict[str, Any] = {}
    if live.get("state") == "running" and (reg.enabled or reg.hostname):
        try:
            router = _router_status(rtr)
        except (libvirt.libvirtError, TimeoutError, RuntimeError) as e:
            out.message = f"The router's guest agent doesn't answer: {e}"
    if router.get("disk_total"):
        out.disk_total_gb = round(int(router["disk_total"]) / 1024 ** 3, 1)
        out.disk_used_gb = round(int(router["disk_used"]) / 1024 ** 3, 1)
    for rec in records.values():  # runs a previous app process left running
        if rec.status == "running" and not task_service.is_running(rec.task_id):
            if rec.id in router.get("mirror_active", []):
                continue
            rc = router.get("runs", {}).get(rec.id)
            if rc is None and not router:
                continue  # router not reachable: don't know
            rec.status = "done" if rc == "0" else "failed" if rc else "interrupted"
            if rec.status == "interrupted":
                rec.error = "The app restarted while it ran: mirror it again (already copied images are kept)"
            _save_record(group.name, rec)
    out.mirrors = sorted(records.values(), key=lambda r: r.started_at or datetime.min, reverse=True)

    if not reg.enabled:
        out.state = "disabled"
        return out
    if live.get("state") != "running":
        out.state, out.message = "stopped", "The router is not running (start the group)"
        return out
    if not router:
        out.state = "error"
        return out
    phase = router.get("phase", "none")
    needs_restart = out.router_memory_mb is not None and out.router_memory_mb < spec.router.effective_memory() * 0.95
    if router.get("healthy") == "1":
        out.state, out.ready = "ready", True
        if needs_restart:
            out.message = (f"The router runs with {out.router_memory_mb} MiB: it gets "
                           f"{spec.router.effective_memory()} MiB at its next restart")
    elif router.get("setup_active") in ("active", "activating"):
        out.state, out.message = "installing", f"Setting up: {_phase_text(router)}"
    elif needs_restart or phase == "restart-required":
        out.state = "restart-required"
        out.message = (f"The router has {out.router_memory_mb} MiB of RAM, the registry needs "
                       f"{spec.router.effective_memory()} MiB: the setup restarts the router")
    elif phase == "failed":
        out.state, out.message = "error", router.get("error") or "The setup failed"
    elif router.get("installed") == "1":
        out.state, out.message = "starting", "Quay is installed but not answering yet"
    else:
        out.state, out.message = "not-installed", "Not set up yet"
    if out.setup_task_id and out.state in ("not-installed", "restart-required", "error"):
        out.state, out.message = "installing", "Setting up (task running)"
    return out


def _running_task(db: Session, group: Group, kind: str) -> Optional[int]:
    task = (db.query(Task).filter(Task.target_type == "registry", Task.target_id == group.id, Task.type == kind,
                                  Task.status.in_(["pending", "running"]))
            .order_by(Task.id.desc()).first())
    return task.id if task is not None and task_service.is_running(task.id) else None


# ---------------------------------------------------------------- setup

class _Progress:
    """Task progress (percent + description line) and cancellation"""

    def __init__(self, db: Session, task: Optional[Task], window: Tuple[int, int] = (0, 100)):
        self.db, self.task = db, task
        self.window = window  # part of the task's 0-100 this work reports into (e.g. a cluster create)
        self.lo, self.hi = window

    def span(self, lo: int, hi: int) -> "_Progress":
        wlo, whi = self.window
        self.lo, self.hi = wlo + (whi - wlo) * lo // 100, wlo + (whi - wlo) * hi // 100
        return self

    def __call__(self, fraction: float, message: Optional[str] = None) -> None:
        if self.task is None:
            return
        task = task_service.get_task(self.db, self.task.id)
        if task is None:
            return
        if message:
            task.description = message[:500]
        pct = self.lo + int((self.hi - self.lo) * max(0.0, min(fraction, 1.0)))
        task.progress = max(task.progress or 0, min(pct, 99))
        self.db.commit()
        task_service._publish(task)

    def check(self) -> None:
        if self.task is not None and task_service.is_cancelled(self.task.id):
            raise RuntimeError("Cancelled")


MIRROR_REGISTRY_SIZE = 1_300_000_000  # mirror-registry-amd64.tar.gz 2.0.x, for the download progress


def _phase_text(st: Dict[str, Any]) -> str:
    phase = st.get("phase") or "starting"
    if phase == "download" and st.get("download"):
        return f"downloading mirror-registry ({int(st['download']) // 1_000_000} MB of ~1300 MB)"
    return {"disk": "formatting the registry disk", "packages": "installing podman",
            "certificate": "making the TLS certificate", "install": "installing Quay (a few minutes)",
            "starting": "starting Quay"}.get(phase, phase)


SETUP_PHASES = {"disk": 0.05, "packages": 0.1, "download": 0.2, "certificate": 0.55, "install": 0.6,
                "certificate-update": 0.9, "starting": 0.92, "ready": 1.0}


def start_setup(db: Session, group: Group) -> Task:
    """Background task: ensure_ready (restart the router if it needs more RAM, install the registry)"""
    running = _running_task(db, group, "registry_setup")
    if running:
        return task_service.get_task(db, running)
    return task_service.start(
        db, TaskCreate(name=f"Set up mirror registry of {group.name}", type="registry_setup", target_type="registry",
                       target_id=group.id, target_name=group.name,
                       description="Download mirror-registry, install Quay on the router"),
        _setup_task, group.id)


def _setup_task(db: Session, task: Task, group_id: int) -> Dict[str, Any]:
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise ValueError("The group no longer exists")
    st = ensure_ready(db, group, task)
    return {"registry": st.url, "uplink": st.uplink_url}


def ensure_ready(db: Session, group: Group, task: Optional[Task] = None,
                 progress: Optional[_Progress] = None) -> RegistryStatus:
    """Router running with the registry's RAM / disk (restarted once if needed), Quay installed and
    answering, CA read back into the spec. Blocking; idempotent (fast when ready)."""
    from app.services.group_service import group_service
    progress = progress or _Progress(db, task)
    with _lock(group.name, "setup"):
        db.refresh(group)
        spec = _spec(group)
        if not spec.router.registry.enabled:
            raise ValueError(f"The mirror registry is not enabled on group {group.name} "
                             "(router.registry.enabled in the group spec)")
        rtr = _rtr(group)
        progress(0, "Starting the router")
        group_service.ensure_running(db, group.id)
        progress.check()
        res = ensure_router_disk(spec)
        if res["restart_required"]:
            progress(0.02, "Restarting the router (more RAM / vCPUs for the registry)")
            group_service.restart_router(db, group)
        db.refresh(group)
        spec = _spec(group)
        if not spec.router.uplink_ip:
            raise RuntimeError("The router's uplink address is not known yet: start the group and retry")
        # script + settings (the uplink address is pinned after the router's first boot)
        group_service._agent_put(rtr, rr.SCRIPT, rr.script().encode())
        group_service._agent_put(rtr, rr.ENV_FILE, rr.env_file(spec).encode())
        _push_credentials(rtr, group.name)
        st = _router_status(rtr)
        if st.get("healthy") != "1":
            if st.get("setup_active") not in ("active", "activating"):
                result = _exec(rtr, f"systemctl reset-failed {rr.SETUP_UNIT} 2>/dev/null; "
                                    f"systemd-run --unit={rr.SETUP_UNIT} --collect /bin/bash {rr.SCRIPT} setup")
                if result["exitcode"] != 0:
                    raise RuntimeError(f"Could not start the registry setup: {result['stderr'].strip()}")
                time.sleep(2)
            deadline = time.monotonic() + SETUP_TIMEOUT
            while True:
                progress.check()
                try:
                    st = _router_status(rtr)
                except (libvirt.libvirtError, TimeoutError) as e:
                    logger.info(f"Registry status of {rtr}: {e}")
                    st = {}
                phase = st.get("phase", "")
                if st.get("healthy") == "1" and phase == "ready":
                    break
                active = st.get("setup_active") in ("active", "activating")
                if st and not active:
                    if phase == "ready" and st.get("healthy") == "1":
                        break
                    raise RuntimeError(f"Registry setup failed ({phase}): {st.get('error') or 'see '
                                       + rr.SETUP_LOG + ' on the router'}")
                frac = SETUP_PHASES.get(phase, 0.0)
                if phase == "download" and st.get("download"):
                    frac += 0.35 * min(int(st["download"]) / MIRROR_REGISTRY_SIZE, 1)
                progress(frac, f"Registry setup: {_phase_text(st)}")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"The registry setup didn't finish in {SETUP_TIMEOUT // 60} min "
                                       f"(phase {phase}); see {rr.SETUP_LOG} on the router")
                time.sleep(POLL)
        _sync_ca(db, group, st.get("ca"))
        progress(1.0, "Registry ready")
    return status(db, group)


def _sync_ca(db: Session, group: Group, ca: Optional[str]) -> None:
    """Store the registry CA in the spec (and the router's metadata), like the WireGuard public key"""
    if not ca:
        return
    db.refresh(group)
    spec = _spec(group)
    if spec.router.registry.ca_pem == ca:
        return
    spec.router.registry.ca_pem = ca
    group.spec = spec.model_dump(mode="json")
    db.commit()
    rtr = _rtr(group)
    try:
        libvirt_client.set_domain_group_metadata(rtr, spec.name, "router", spec=group.spec)
    except libvirt.libvirtError as e:
        logger.warning(f"Could not update {rtr} metadata: {e}")
    from app.events import event_bus
    event_bus.publish({"kind": "group", "event": "updated", "id": group.id, "name": group.name,
                       "status": group.status})


# ---------------------------------------------------------------- mirroring

def _channel(version: str) -> str:
    minor = ".".join(version.split("-")[0].split(".")[:2])
    return f"{'candidate' if '-' in version else 'stable'}-{minor}"


def image_set_config(request: MirrorRequest) -> str:
    """oc-mirror v2 ImageSetConfiguration for a request"""
    mirror: Dict[str, Any] = {}
    if request.openshift_version:
        v = request.openshift_version
        mirror["platform"] = {"channels": [{"name": _channel(v), "minVersion": v, "maxVersion": v, "type": "ocp"}],
                              "graph": False}
    if request.operators:
        mirror["operators"] = []
        for cat in request.operators:
            packages = []
            for p in cat.packages:
                entry: Dict[str, Any] = {"name": p.name}
                if p.channel:
                    entry["channels"] = [{"name": p.channel}]
                packages.append(entry)
            mirror["operators"].append({"catalog": _catalog(request, cat.catalog), "packages": packages})
    if request.additional_images:
        mirror["additionalImages"] = [{"name": i} for i in request.additional_images]
    return yaml.safe_dump({"kind": "ImageSetConfiguration", "apiVersion": "mirror.openshift.io/v2alpha1",
                           "mirror": mirror}, sort_keys=False)


def _catalog(request: MirrorRequest, catalog: Optional[str]) -> str:
    if catalog:
        return catalog
    minor = request.minor()
    if not minor:
        raise ValueError("Operators without a catalog need openshift_version (the default catalog is "
                         "registry.redhat.io/redhat/redhat-operator-index:v<minor>)")
    return f"registry.redhat.io/redhat/redhat-operator-index:v{minor}"


def _request_id(spec: GroupSpec, request: MirrorRequest) -> str:
    canonical = request.model_copy(deep=True)
    for cat in canonical.operators:
        cat.catalog = _catalog(request, cat.catalog)
    key = json.dumps({"host": _hosts(spec)[0], "request": canonical.model_dump(mode="json")}, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:12]


_PROGRESS = re.compile(r"(?:✓|✗|copying image|images? to copy)\D{0,40}?(\d+)\s*/\s*(\d+)")


def _parse_progress(log: str) -> Tuple[Optional[int], Optional[int], str]:
    """(done, total, last line) from oc-mirror's output"""
    done = total = None
    last = ""
    for line in log.splitlines():
        text = line.strip()
        if text:
            last = text
        m = _PROGRESS.search(text)
        if m:
            d, t = int(m.group(1)), int(m.group(2))
            if t >= (total or 0):
                done, total = d, t
    last = re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s+", "", last)
    return done, total, last[:200]


def _pull_auths() -> Dict[str, Any]:
    """The OpenShift pull secret's auths ({} when none is configured)"""
    from app.services.openshift_service import openshift_service
    try:
        return json.loads(openshift_service.pull_secret()).get("auths", {})
    except ValueError:
        return {}


def ensure_mirrored(db: Session, group: Group, request: MirrorRequest, task: Optional[Task] = None,
                    window: Tuple[int, int] = (0, 100)) -> MirrorResult:
    """Copy `request` into the group's registry (blocking, idempotent: a request mirrored before only
    reads its results back). Progress / cancellation through `task` (its progress moves within `window`)."""
    if not (request.openshift_version or request.operators or request.additional_images):
        raise ValueError("Nothing to mirror: give an OpenShift version, operators or images")
    progress = _Progress(db, task, window)
    ensure_ready(db, group, task, progress.span(0, 10))
    spec = _spec(group)
    pull = _pull_auths()
    if (request.openshift_version or request.operators) and not pull:
        raise ValueError("Mirroring OpenShift releases / operators needs the OpenShift pull secret "
                         "(add it in the Create cluster dialog)")
    rtr = _rtr(group)
    run_id = _request_id(spec, request)
    run_dir = f"{rr.MIRROR_RUNS}/{run_id}"
    record = MirrorRecord.model_validate({**_load_records(group.name).get(run_id, {}), "id": run_id,
                                          "status": "running", "openshift_version": request.openshift_version,
                                          "operators": [c.model_dump() for c in request.operators],
                                          "additional_images": request.additional_images,
                                          "task_id": task.id if task else None, "error": None})
    record.started_at = _now()
    record.finished_at = None
    progress.span(10, 99)
    with _lock(group.name, "mirror"):
        st = _router_status(rtr)
        rc = st["runs"].get(run_id)
        active = run_id in st["mirror_active"]
        if rc == "0" and not active:
            progress(0.95, "Already mirrored: reading the results")
        else:
            if not active:
                # another run (e.g. left by a previous app process) holds oc-mirror's cache: wait for it
                others = [a for a in st["mirror_active"] if a != run_id]
                while others:
                    progress.check()
                    progress(0, f"Waiting for mirror run {others[0]} to finish")
                    time.sleep(POLL)
                    others = [a for a in _router_status(rtr)["mirror_active"] if a != run_id]
                _start_run(rtr, run_id, run_dir, request, {**pull, **_auths(spec, group.name)["auths"]})
            _save_record(group.name, record)
            try:
                rc = _follow_run(rtr, run_id, run_dir, progress, record)
            except Exception as e:
                record.status = "cancelled" if str(e) == "Cancelled" else "failed"
                record.error, record.finished_at = str(e)[:1000], _now()
                _save_record(group.name, record)
                if record.status == "cancelled":
                    _exec(rtr, f"systemctl stop vmm-mirror-{run_id} 2>/dev/null; true")
                raise
            if rc != "0":
                tail = _exec(rtr, f"tail -n 15 {run_dir}/log; for f in {run_dir}/workspace/working-dir/logs/"
                                  "mirroring_errors_*; do [ -f \"$f\" ] && head -n 20 \"$f\"; done; true")["stdout"]
                record.status, record.finished_at = "failed", _now()
                record.error = f"oc-mirror exited with {rc}: {tail.strip()[-1500:]}"
                _save_record(group.name, record)
                raise RuntimeError(record.error)
        result = _collect(rtr, spec, group.name, run_dir, request)
        record.status, record.finished_at, record.error = "done", _now(), None
        _save_record(group.name, record)
    progress(1.0, "Mirrored")
    return result


def _start_run(rtr: str, run_id: str, run_dir: str, request: MirrorRequest, auths: Dict[str, Any]) -> None:
    from app.services.group_service import group_service
    group_service._agent_put(rtr, f"{run_dir}/isc.yaml", image_set_config(request).encode())
    # pull secret + registry credentials: tmpfs, 0600, deleted by the unit when oc-mirror exits
    result = _exec(rtr, f"umask 077 && mkdir -p /run/vmm-mirror-{run_id} && cat > /run/vmm-mirror-{run_id}/auth.json",
                   input_data=json.dumps({"auths": auths}).encode())
    if result["exitcode"] != 0:
        raise RuntimeError(f"Could not write the mirror credentials on the router: {result['stderr'].strip()}")
    result = _exec(rtr, f"systemctl reset-failed vmm-mirror-{run_id} 2>/dev/null; "
                        f"systemd-run --unit=vmm-mirror-{run_id} --collect /bin/bash {rr.SCRIPT} mirror {run_id}")
    if result["exitcode"] != 0:
        _exec(rtr, f"rm -rf /run/vmm-mirror-{run_id}")
        raise RuntimeError(f"Could not start oc-mirror on the router: {result['stderr'].strip()}")
    logger.info(f"oc-mirror run {run_id} started on {rtr}")


def _follow_run(rtr: str, run_id: str, run_dir: str, progress: _Progress, record: MirrorRecord) -> str:
    """Poll the detached oc-mirror unit until it exits; returns its rc"""
    deadline = time.monotonic() + MIRROR_TIMEOUT
    errors = 0
    while True:
        progress.check()
        try:
            out = _exec(rtr, f"systemctl is-active -q vmm-mirror-{run_id} && echo ACTIVE; "
                             f"echo \"RC=$(cat {run_dir}/rc 2>/dev/null)\"; "
                             f"grep -aE '✓|✗|copying image|images? to copy|ERROR|WARN|INFO' {run_dir}/log 2>/dev/null"
                             " | tail -n 400", timeout=30)["stdout"]
            errors = 0
        except (libvirt.libvirtError, TimeoutError) as e:
            errors += 1
            if errors > 30:
                raise RuntimeError(f"Lost the router's guest agent while mirroring: {e}")
            time.sleep(POLL)
            continue
        active = out.startswith("ACTIVE")
        rc = next((line[3:] for line in out.splitlines() if line.startswith("RC=")), "")
        done, total, last = _parse_progress(out)
        if total:
            record.images = total
            progress(done / total, f"oc-mirror: {done} / {total} images — {last}")
        elif last:
            progress(0.0, f"oc-mirror: {last}")
        if not active:
            return rc or "1"
        if time.monotonic() > deadline:
            raise TimeoutError(f"oc-mirror still running after {MIRROR_TIMEOUT // 3600} h")
        time.sleep(POLL)


def _collect(rtr: str, spec: GroupSpec, name: str, run_dir: str, request: MirrorRequest) -> MirrorResult:
    """Read oc-mirror's cluster-resources back"""
    out = _exec(rtr, f"cd {run_dir}/workspace/working-dir/cluster-resources 2>/dev/null || exit 0; "
                     "for f in *; do [ -f \"$f\" ] && echo \"$f $(base64 -w0 \"$f\")\"; done", timeout=60)["stdout"]
    files: Dict[str, str] = {}
    for line in out.splitlines():
        fname, _, data = line.partition(" ")
        try:
            files[fname] = base64.b64decode(data).decode()
        except (ValueError, UnicodeDecodeError):
            continue
    docs: List[str] = []
    for fname in sorted(files):
        text = files[fname]
        if fname.endswith((".yaml", ".yml")):
            docs += [d for d in (part.strip() for part in re.split(r"^---\s*$", text, flags=re.M)) if d]
        elif fname.endswith(".json") and fname.rsplit(".", 1)[0] + ".yaml" not in files:
            try:
                docs.append(yaml.safe_dump(json.loads(text), sort_keys=False).strip())
            except ValueError:
                pass
    digest_sources: List[ImageDigestSource] = []
    catalog_sources: Dict[str, str] = {}
    catalogs = {_catalog(request, c.catalog) for c in request.operators}
    for doc in docs:
        try:
            obj = yaml.safe_load(doc) or {}
        except yaml.YAMLError:
            continue
        kind = obj.get("kind")
        if kind == "ImageDigestMirrorSet":
            for m in (obj.get("spec") or {}).get("imageDigestMirrors") or []:
                if m.get("source") and m.get("mirrors"):
                    digest_sources.append(ImageDigestSource(source=m["source"], mirrors=list(m["mirrors"])))
        elif kind == "CatalogSource":
            image = (obj.get("spec") or {}).get("image") or ""
            for src in catalogs:
                path = src.split("/", 1)[-1]
                if image.endswith("/" + path) or image.split("/", 1)[-1] == path:
                    catalog_sources[src] = obj["metadata"]["name"]
    # release mirrors first (install-config imageDigestSources)
    digest_sources.sort(key=lambda d: 0 if "openshift-release-dev" in d.source else 1)
    hosts = _hosts(spec)
    return MirrorResult(registry_host=hosts[0], uplink_registry_host=hosts[1], ca_pem=spec.router.registry.ca_pem or "",
                        pull_secret_fragment=_auths(spec, name), image_digest_sources=digest_sources,
                        cluster_resources=docs, catalog_sources=catalog_sources)


def start_mirror(db: Session, group: Group, request: MirrorRequest) -> Task:
    """POST /groups/{id}/registry/mirror: ensure_mirrored in a task. The task result is a summary (never
    the credentials)."""
    if not _spec(group).router.registry.enabled:
        raise ValueError("Enable the mirror registry first")
    if not (request.openshift_version or request.operators or request.additional_images):
        raise ValueError("Nothing to mirror: give an OpenShift version, operators or images")
    if (request.openshift_version or request.operators) and not _pull_auths():
        raise ValueError("Mirroring OpenShift releases / operators needs the OpenShift pull secret "
                         "(add it in the Create cluster dialog)")
    if request.operators:
        for cat in request.operators:
            _catalog(request, cat.catalog)
    what = ", ".join(filter(None, [
        f"OpenShift {request.openshift_version}" if request.openshift_version else "",
        f"{sum(len(c.packages) for c in request.operators)} operator(s)" if request.operators else "",
        f"{len(request.additional_images)} image(s)" if request.additional_images else ""]))
    return task_service.start(
        db, TaskCreate(name=f"Mirror into {group.name} registry", type="registry_mirror", target_type="registry",
                       target_id=group.id, target_name=group.name, description=what),
        _mirror_task, group.id, request.model_dump())


def _mirror_task(db: Session, task: Task, group_id: int, request: Dict[str, Any]) -> Dict[str, Any]:
    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise ValueError("The group no longer exists")
    result = ensure_mirrored(db, group, MirrorRequest.model_validate(request), task)
    return {"registry": result.registry_host, "uplink_registry": result.uplink_registry_host,
            "image_digest_sources": [d.model_dump() for d in result.image_digest_sources],
            "catalog_sources": result.catalog_sources, "cluster_resources": len(result.cluster_resources)}


def ca_pem(group: Group) -> Optional[str]:
    return _spec(group).router.registry.ca_pem

