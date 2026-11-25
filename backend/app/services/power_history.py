"""Who powered a VM on / off / rebooted it, and when (GET /vms/{id}/power-history).

Three sources, merged by time:
- the app itself: every libvirt start / shutdown / destroy / reboot it sends (libvirt_client), tagged with the task or
  API request that asked for it;
- the groups' BMC emulators (bmc_emulator): every Redfish power action and virtual media change a BMC client (e.g. a
  hub's metal3 / Ironic driving an ACM spoke) sent, from `<group dir>/bmc-actions.jsonl` (another process: a file);
- libvirt lifecycle events while the app holds a connection (started, shut down by the guest or by the host, crashed,
  guest reboots). The connection is opened on demand, so this part has gaps while nothing is open.

A reboot with no app or BMC entry around it came from inside the guest (systemd, the machine-config daemon, a
watchdog). The app never powers machines on its own: only tasks and API requests do, and they show here.
In memory (app + libvirt entries, the last MAX_ENTRIES per VM); the BMC file survives restarts.
"""
import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_ENTRIES = 200
BMC_ACTIONS_FILE = "bmc-actions.jsonl"
BMC_ACTIONS_MAX_BYTES = 1024 * 1024

_lock = threading.Lock()
_entries: Dict[str, Deque[Dict[str, Any]]] = {}
_local = threading.local()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_actor(actor: Optional[str]) -> None:
    """Who the current thread works for (a task name, an API request): stored with the app's power actions"""
    _local.actor = actor


def actor() -> str:
    return getattr(_local, "actor", None) or "API request"


def record(vm: str, event: str, source: str, detail: Optional[str] = None) -> None:
    entry = {"time": _now(), "event": event, "source": source}
    if detail:
        entry["detail"] = detail
    with _lock:
        _entries.setdefault(vm, deque(maxlen=MAX_ENTRIES)).append(entry)
    logger.info(f"Power history {vm}: {event} ({source}{': ' + detail if detail else ''})")


def app_action(vm: str, event: str) -> None:
    record(vm, event, "vm-manager", actor())


def bmc_actions(path: Path, identities: List[str]) -> List[Dict[str, Any]]:
    """The BMC emulator's entries about one VM (identity = its name or UUID)"""
    result = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return result
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("identity") in identities:
            result.append({"time": entry.get("time"), "event": entry.get("action") or "?",
                           "source": f"BMC (Redfish client {entry.get('client') or '?'})",
                           "detail": entry.get("detail")})
    return result


def history(vm: str, uuid: Optional[str] = None, bmc_file: Optional[Path] = None) -> List[Dict[str, Any]]:
    with _lock:
        items = list(_entries.get(vm) or [])
    if bmc_file is not None:
        items += bmc_actions(bmc_file, [i for i in (vm, uuid) if i])
    items.sort(key=lambda e: e.get("time") or "")
    return items


def summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts per source of the reboots / power-offs / power-ons, and the last one"""
    counts: Dict[str, int] = {}
    for e in items:
        key = "guest" if e["source"] in ("guest", "libvirt") else e["source"].split(" (")[0]
        counts[key] = counts.get(key, 0) + 1
    return {"entries": len(items), "by_source": counts, "last": items[-1] if items else None}
