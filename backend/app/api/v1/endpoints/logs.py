"""Log viewer + diagnostics (docs/logs.md): VM logs and inventory through the guest agent, cluster pod logs.

Logs can hold secrets: /vms/{id}/logs and /clusters/{id}/logs are in auth_middleware.SECRET_READS_RE (no viewers).
Diagnostics and the pod list stay readable by viewers (inventory / states); the raw recent journal errors in
diagnostics are dropped for them.
"""
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.v1.endpoints.auth import current_user
from app.database import get_db
from app.services import log_service
from app.services.cluster_service import cluster_service
from app.services.log_service import LogError
from app.services.vm_service import vm_service

router = APIRouter()


def _vm_name(db: Session, vm_id: int) -> str:
    vm = vm_service.get_vm(db, vm_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")
    return vm.name


def _cluster(db: Session, cluster_id: int):
    cluster = cluster_service.get_cluster(db, cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return cluster


def _run(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return func(*args, **kwargs)
    except LogError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _answer(raw: Dict[str, Any], f: Dict[str, Any], fmt: str, extra: Dict[str, Any]) -> Any:
    result = _run(log_service.analyze, raw["lines"], raw.get("levels") or None, **f)
    if fmt == "text":
        return PlainTextResponse(log_service.as_text(result))
    return {**extra, "source": raw["source"], "command": raw["command"],
            **{k: raw[k] for k in ("pods", "notes") if k in raw}, **result}


def _filters(level: Optional[str], include: Optional[str], exclude: Optional[str], problems_only: bool,
             category: Optional[str], context: int, max_entries: int) -> Dict[str, Any]:
    return {"level": level, "include": include, "exclude": exclude, "problems_only": problems_only,
            "category": category, "context": context, "max_entries": max_entries}


FILTER_DOC = """
Filters (same for every source): level = error | warning (that level or worse), include / exclude = regex
(case-insensitive), problems_only = only lines matching a known-problem rule (GET /logs/rules), category =
comma-separated categories or rule ids (e.g. 'images,pods' or 'oom-killer'), context = lines before/after each
match (0-20), max_entries = cap on returned lines (most recent kept). Every line is annotated: level, rules[];
summary = counts per category with the hint. format=text returns the selected lines as plain text."""


@router.get("/logs/rules")
def log_rules():
    """The built-in known-problems ruleset: categories, and per rule its regex, level and plain-words hint"""
    return log_service.rules_info()


VM_LOGS_DOC = """Logs of a VM through the QEMU guest agent (no SSH): the journal (journalctl, priority from the
journal), the kernel ring buffer (dmesg -T -x; journalctl -k for older boots / a time window), cloud-init (status
--long + its logs) or any file (tail)."""


@router.get("/vms/{vm_id}/logs", description=VM_LOGS_DOC + FILTER_DOC)
def vm_logs(vm_id: int, db: Session = Depends(get_db),
            source: str = Query("journal", description="journal | dmesg | cloud-init | file"),
            lines: int = Query(log_service.DEFAULT_LINES, ge=1, le=log_service.MAX_LINES,
                               description="lines fetched from the source (tail)"),
            unit: Optional[List[str]] = Query(None, description="journal: systemd unit(s), e.g. kubelet"),
            priority: Optional[str] = Query(None, description="journal: journalctl -p (err, warning, …)"),
            since: Optional[str] = Query(None, description="journal / dmesg: e.g. '-1h', 'today', '2026-10-11 10:00'"),
            until: Optional[str] = None,
            boot: Optional[str] = Query(None, description="journal / dmesg: 0 = current boot, -1 = previous, all"),
            grep: Optional[str] = Query(None, description="journal: journalctl --grep (in the guest)"),
            path: Optional[str] = Query(None, description="file: absolute path inside the VM"),
            level: Optional[str] = None, include: Optional[str] = None, exclude: Optional[str] = None,
            problems_only: bool = False, category: Optional[str] = None, context: int = Query(2, ge=0, le=20),
            max_entries: int = Query(2000, ge=1, le=log_service.MAX_ENTRIES), format: str = "json"):
    name = _vm_name(db, vm_id)
    raw = _run(log_service.vm_logs, name, source, lines, unit, priority, since, until, boot, grep, path)
    return _answer(raw, _filters(level, include, exclude, problems_only, category, context, max_entries), format,
                   {"vm": name})



@router.get("/vms/{vm_id}/diagnostics")
def vm_diagnostics(vm_id: int, request: Request, db: Session = Depends(get_db)):
    """Inventory of a VM through the guest agent: OS / kernel / cmdline, dmidecode BIOS + system (firmware
    versions), lspci -nnk, NICs (ip addr + ethtool -i driver / firmware-version / bus-info, VFs), lsblk, df, SR-IOV
    PFs / IOMMU groups / vfio devices, failed systemd units, recent journal errors, plus findings in plain words.
    Missing tools are listed, not fatal. Where guest-exec is forbidden (EL defaults) only the agent's own info
    commands are returned (exec: false)."""
    name = _vm_name(db, vm_id)
    result = _run(log_service.diagnostics, name)
    user = current_user(request)
    if user is not None and getattr(user, "role", "admin") != "admin":
        result.pop("recent_errors", None)  # raw log lines: not for viewers
    return result


@router.get("/clusters/{cluster_id}/pods")
def cluster_pods(cluster_id: int, db: Session = Depends(get_db),
                 match: Optional[str] = Query(None, description="substring of namespace/name, e.g. 'sriov'"),
                 namespace: Optional[str] = None, selector: Optional[str] = Query(None, description="label selector"),
                 problems_only: bool = Query(False, description="only unhealthy pods: not running, a container waiting / failed / not ready, or restarted in the last hour")):
    """Find pods (kubectl / oc get pods): phase, ready, restarts, node, containers and their problems
    (CrashLoopBackOff, ImagePullBackOff, OOMKilled, …)."""
    return _run(log_service.list_pods, _cluster(db, cluster_id), match, namespace, selector, problems_only)


POD_LOGS_DOC = """Pod logs: one pod (namespace + pod), or every pod matching a selector / namespace / name substring
(up to 30 pods, every container, lines prefixed [namespace/pod/container]). Status lines (waiting reasons, OOMKilled)
and the pods' events are added: ImagePullBackOff or FailedScheduling never show in a container log."""


@router.get("/clusters/{cluster_id}/logs", description=POD_LOGS_DOC + FILTER_DOC)
def cluster_logs(cluster_id: int, db: Session = Depends(get_db),
                 namespace: Optional[str] = None, pod: Optional[str] = None,
                 selector: Optional[str] = Query(None, description="label selector"),
                 match: Optional[str] = Query(None, description="pods whose namespace/name contains this"),
                 container: Optional[str] = None, previous: bool = False,
                 tail: int = Query(500, ge=1, le=log_service.MAX_LINES, description="lines per container"),
                 since: Optional[str] = Query(None, description="e.g. 15m, 2h"),
                 events: bool = Query(True, description="add the pods' events and container status lines"),
                 level: Optional[str] = None, include: Optional[str] = None, exclude: Optional[str] = None,
                 problems_only: bool = False, category: Optional[str] = None, context: int = Query(2, ge=0, le=20),
                 max_entries: int = Query(2000, ge=1, le=log_service.MAX_ENTRIES), format: str = "json"):
    cluster = _cluster(db, cluster_id)
    raw = _run(log_service.pod_logs, cluster, namespace, pod, selector, match, container, previous, tail, since,
               events)
    return _answer(raw, _filters(level, include, exclude, problems_only, category, context, max_entries), format,
                   {"cluster": cluster.name})

