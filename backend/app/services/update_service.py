"""Version and updates from GitHub releases (docs/updates.md).

Two install modes:
- release: the code runs from /opt/vm-manager/releases/<version> (via the /opt/vm-manager/current symlink),
  installed and updated by scripts/vm-manager-update (root, through `sudo vm-manager-update update` or the
  helper action `self-update`, started by the "Update now" button);
- checkout: a git clone (development): updated with `git pull` + scripts/setup.sh.

Release channels: stable = the latest GitHub release; nightly = the rolling `nightly` pre-release built from main.
The GitHub API is asked at most every CHECK_INTERVAL (anonymous: 60 requests/h per IP).
"""
import logging
import os
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import ROOT_DIR, settings
from app.services.helper_service import run_helper

logger = logging.getLogger(__name__)

RELEASES_DIR = "/opt/vm-manager/releases"
UPDATER = "/usr/local/sbin/vm-manager-update"
UPDATE_LOG = "/var/log/vm-manager-update.log"
CHECK_INTERVAL = 6 * 3600
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$")
ASSET_RE = re.compile(r"^vm-manager-(.+)\.tar\.gz$")

_lock = threading.Lock()
_cache: Dict[str, Any] = {"at": 0.0, "channel": None, "result": None}


def install_mode() -> str:
    root = str(ROOT_DIR.resolve())
    return "release" if root.startswith(RELEASES_DIR + "/") else "checkout"


def version_key(version: str) -> Tuple:
    """Sortable key: 0.2.0 > 0.2.0-nightly.20261009.abc > 0.1.9 (a pre-release sorts before its release)"""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-(.*))?$", version or "")
    if not m:
        return (0, 0, 0, 0, "")
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


def _release_info(rel: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """GitHub release JSON -> {version, tag, notes, url, published_at} (version from the tarball asset name)"""
    for asset in rel.get("assets") or []:
        m = ASSET_RE.match(asset.get("name") or "")
        if m and VERSION_RE.match(m.group(1)):
            return {"version": m.group(1), "tag": rel.get("tag_name"), "notes": (rel.get("body") or "")[:20000],
                    "url": rel.get("html_url"), "published_at": rel.get("published_at") or rel.get("created_at"),
                    "prerelease": bool(rel.get("prerelease"))}
    return None


def fetch_latest(channel: str) -> Dict[str, Any]:
    """Latest release of the channel from the GitHub API"""
    base = f"https://api.github.com/repos/{settings.UPDATE_REPO}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"vm-manager/{settings.APP_VERSION}"}
    with httpx.Client(timeout=15, headers=headers, follow_redirects=True) as client:
        if channel == "nightly":
            r = client.get(f"{base}/releases/tags/nightly")
        else:
            r = client.get(f"{base}/releases/latest")
        if r.status_code == 404:
            raise LookupError(f"no {channel} release published yet in {settings.UPDATE_REPO}")
        r.raise_for_status()
    info = _release_info(r.json())
    if info is None:
        raise LookupError(f"the {channel} release of {settings.UPDATE_REPO} has no vm-manager-<version>.tar.gz")
    return info


def status(refresh: bool = False, channel: Optional[str] = None) -> Dict[str, Any]:
    """GET /hosts/update: running version, latest release of the channel, how to update"""
    channel = channel if channel in ("stable", "nightly") else settings.UPDATE_CHANNEL
    mode = install_mode()
    current = settings.APP_VERSION
    result: Dict[str, Any] = {
        "version": current, "mode": mode, "channel": channel, "repo": settings.UPDATE_REPO,
        "check_enabled": settings.UPDATE_CHECK, "latest": None, "available": False, "error": None,
        "checked_at": None, "can_update": mode == "release", "updating": _updating(),
        "command": (f"sudo vm-manager-update update --channel {channel}" if mode == "release"
                    else f"cd {ROOT_DIR} && git pull && scripts/setup.sh"),
        "log": UPDATE_LOG,
    }
    if not settings.UPDATE_CHECK and not refresh:
        return result
    with _lock:
        fresh = (_cache["channel"] == channel and _cache["result"] is not None
                 and time.time() - _cache["at"] < CHECK_INTERVAL)
        if refresh or not fresh:
            try:
                _cache["result"] = {"latest": fetch_latest(channel), "error": None}
            except (httpx.HTTPError, LookupError, ValueError) as e:
                _cache["result"] = {"latest": None, "error": f"Cannot check {settings.UPDATE_REPO}: {e}"}
            _cache.update(at=time.time(), channel=channel)
        result.update(_cache["result"])
        result["checked_at"] = _cache["at"]
    latest = result["latest"]
    if latest:
        if channel == "nightly":
            result["available"] = latest["version"] != current
        else:
            result["available"] = version_key(latest["version"]) > version_key(current)
    return result


def _updating() -> bool:
    """An update started by the button is still running (its transient unit exists)"""
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", "vm-manager-update"], timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def start_update(version: str, channel: str) -> Dict[str, Any]:
    """POST /hosts/update: run the updater detached through the helper (it restarts this service)"""
    if install_mode() != "release":
        raise ValueError("This instance runs from a git checkout: update it with git pull + scripts/setup.sh")
    if not VERSION_RE.match(version or ""):
        raise ValueError(f"Invalid version {version!r}")
    if channel not in ("stable", "nightly"):
        raise ValueError(f"Invalid channel {channel!r}")
    out = run_helper(["self-update", version, channel], timeout=30)
    logger.info(f"update to {version} ({channel}) started: {out.strip()}")
    return {"started": True, "version": version, "log": UPDATE_LOG,
            "message": f"Updating to {version}: the app restarts in a minute or two (log: {UPDATE_LOG})"}


def releases() -> List[str]:
    """Installed releases (release mode), newest first"""
    try:
        names = [n for n in os.listdir(RELEASES_DIR) if VERSION_RE.match(n)]
    except OSError:
        return []
    return sorted(names, key=version_key, reverse=True)
