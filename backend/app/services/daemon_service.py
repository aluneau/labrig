"""Start / stop the libvirt daemon(s) through systemd, and report their state without connecting.

Connecting to qemu:///system while only the daemon's sockets listen would socket-activate it, so
the state comes from `systemctl show`. Start/stop call `systemctl start/stop` as the app's user:
scripts/setup.sh installs a polkit rule letting the `libvirt` group do that for libvirt units only.

Two layouts exist:
- monolithic: libvirtd.service (+ libvirtd.socket, -ro, -admin). Arch, Debian/Ubuntu.
- modular: one daemon per driver, virtqemud, virtnetworkd, virtstoraged... RHEL 9/10, Fedora.
"""
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import libvirt

from app.config import settings
from app.events import event_bus
from app.libvirt_client import libvirt_client, LibvirtUnavailable

logger = logging.getLogger(__name__)

SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MODULAR_DRIVERS = ["qemu", "network", "storage", "nodedev", "secret", "nwfilter", "interface"]
# Started explicitly (not only their sockets): networks / VMs / pools marked autostart start with them
MODULAR_SERVICES = ["virtqemud.service", "virtnetworkd.service", "virtstoraged.service"]


def _daemon_units(daemon: str) -> List[str]:
    return [f"{daemon}.service", f"{daemon}.socket", f"{daemon}-ro.socket", f"{daemon}-admin.socket"]


MONOLITHIC_UNITS = _daemon_units("libvirtd")
MODULAR_UNITS = [u for drv in MODULAR_DRIVERS for u in _daemon_units(f"virt{drv}d")]
MAIN = {"monolithic": "libvirtd", "modular": "virtqemud"}


class DaemonError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _os_family() -> str:
    try:
        with open("/etc/os-release") as f:
            info = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
    except OSError:
        return ""
    ids = " ".join(info.get(k, "").strip('"') for k in ("ID", "ID_LIKE")).split()
    return "rhel" if {"rhel", "fedora", "centos"} & set(ids) else ""


class DaemonService:
    def __init__(self):
        self._transition: Optional[str] = None  # "starting" | "stopping" while we drive systemd
        self._last_state: Optional[str] = None
        self._action_lock = threading.Lock()
        self._systemctl = shutil.which("systemctl", path=SYSTEM_PATH)

    def manageable(self) -> bool:
        """Start/stop only make sense for the local system daemon"""
        return self._systemctl is not None and libvirt_client.uri.strip() == "qemu:///system"

    # systemd

    def _show(self, units: List[str]) -> List[Dict[str, str]]:
        """`systemctl show` properties of each unit, in order (output is not localized)"""
        result = subprocess.run(
            [self._systemctl, "show", "--property=Id,LoadState,ActiveState,UnitFileState", "--", *units],
            capture_output=True, text=True, timeout=15, env={**os.environ, "LC_ALL": "C"},
        )
        blocks = []
        for block in result.stdout.strip().split("\n\n"):
            props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            blocks.append(props)
        if len(blocks) != len(units):
            raise DaemonError(500, f"unexpected systemctl output: {result.stderr.strip() or result.stdout[:200]}")
        return blocks

    def _systemctl_run(self, verb: str, units: List[str]) -> None:
        if not units:
            return
        result = subprocess.run(
            [self._systemctl, "--no-ask-password", verb, "--", *units],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            logger.error(f"systemctl {verb} {' '.join(units)} failed: {err}")
            if "auth" in err.lower() or "polkit" in err.lower() or "access denied" in err.lower():
                raise DaemonError(403, f"Not allowed to {verb} libvirt ({err}). Re-run scripts/setup.sh: it installs "
                                       f"the polkit rule letting the 'libvirt' group start/stop libvirt.")
            raise DaemonError(500, f"systemctl {verb} failed: {err}")

    def _units(self) -> Dict[str, Dict[str, str]]:
        names = MONOLITHIC_UNITS + MODULAR_UNITS
        return {name: props for name, props in zip(names, self._show(names))}

    @staticmethod
    def _active(units: Dict[str, Dict[str, str]], name: str) -> bool:
        return units.get(name, {}).get("ActiveState") == "active"

    def _mode(self, units: Dict[str, Dict[str, str]]) -> str:
        """Which layout this host uses: the one running, else the one enabled at boot, else the distro default"""
        if settings.LIBVIRT_DAEMON_MODE in ("monolithic", "modular"):
            return settings.LIBVIRT_DAEMON_MODE
        for mode, main in MAIN.items():
            if self._active(units, f"{main}.service") or self._active(units, f"{main}.socket"):
                return mode
        for mode, main in MAIN.items():
            if any(units.get(f"{main}.{t}", {}).get("UnitFileState") == "enabled" for t in ("service", "socket")):
                return mode
        if units.get("libvirtd.service", {}).get("LoadState") != "loaded":
            return "modular"
        if units.get("virtqemud.service", {}).get("LoadState") != "loaded":
            return "monolithic"
        return "modular" if _os_family() == "rhel" else "monolithic"

    # state

    def status(self, probe: bool = True) -> Dict[str, Any]:
        """Current state; publishes a connection event when it changed.

        probe: for non-systemd URIs (test://, remote), try to connect to find out.
        """
        base = {
            "connected": libvirt_client.is_alive(),
            "idle_timeout_minutes": settings.LIBVIRT_IDLE_TIMEOUT,
            "helper_installed": os.path.isfile(settings.HELPER_PATH),
            "dhcp_release_available": shutil.which("dhcp_release", path=SYSTEM_PATH) is not None,
            "uri": libvirt_client.uri,
        }
        if not self.manageable():
            state = "running" if base["connected"] else None
            if state is None and probe:
                try:
                    libvirt_client.connect()
                    state = "running"
                except LibvirtUnavailable:
                    state = "stopped"
                except libvirt.libvirtError:
                    state = "running"  # reachable, but refused us: pages will show the error
            result = {**base, "state": self._transition or state or self._last_state or "stopped",
                      "manageable": False, "mode": None, "daemon_active": base["connected"], "units": []}
            self._publish(result["state"])
            return result

        units = self._units()
        mode = self._mode(units)
        main = MAIN[mode]
        service = units[f"{main}.service"].get("ActiveState")
        if self._transition:
            state = self._transition
        elif service == "activating":
            state = "starting"
        elif service == "deactivating":
            state = "stopping"
        elif service == "active" or self._active(units, f"{main}.socket"):
            state = "running"  # with only the socket listening, the first connection starts the daemon
        else:
            state = "stopped"
        listed = [n for n in (MONOLITHIC_UNITS if mode == "monolithic" else MODULAR_UNITS)
                  if units[n].get("LoadState") == "loaded"]
        self._publish(state)
        return {
            **base, "state": state, "manageable": True, "mode": mode, "daemon_active": service == "active",
            "units": [{"name": n, "active_state": units[n].get("ActiveState", ""),
                       "enabled": units[n].get("UnitFileState") or None} for n in listed],
        }

    def _publish(self, state: str) -> None:
        if state != self._last_state:
            self._last_state = state
            event_bus.publish({"kind": "connection", "event": "state", "state": state})

    def _set_transition(self, value: Optional[str]) -> None:
        self._transition = value
        if value:
            self._publish(value)

    # actions

    def start(self) -> Dict[str, Any]:
        if not self.manageable():
            raise DaemonError(400, f"Starting libvirt is only supported for qemu:///system with systemd "
                                   f"(LIBVIRT_URI is {libvirt_client.uri})")
        if not self._action_lock.acquire(blocking=False):
            raise DaemonError(409, "libvirt is already being started or stopped")
        try:
            self._set_transition("starting")
            units = self._units()
            mode = self._mode(units)
            loaded = [n for n in (MONOLITHIC_UNITS if mode == "monolithic" else MODULAR_UNITS)
                      if units[n].get("LoadState") == "loaded"]
            if mode == "monolithic":
                todo = loaded  # libvirtd.service + its sockets
            else:
                todo = [n for n in loaded if n.endswith(".socket") or n in MODULAR_SERVICES]
            logger.info(f"Starting libvirt ({mode}): {' '.join(todo)}")
            self._systemctl_run("start", todo)
            deadline = time.monotonic() + 30
            while True:
                try:
                    libvirt_client.connect()
                    break
                except LibvirtUnavailable:
                    if time.monotonic() > deadline:
                        raise DaemonError(500, "libvirt was started but does not accept connections")
                    time.sleep(0.5)
        finally:
            self._set_transition(None)
            self._action_lock.release()
        return self.status()

    def running_vms(self) -> List[str]:
        try:
            return libvirt_client.running_vm_names()
        except LibvirtUnavailable:
            return []

    def stop(self) -> Dict[str, Any]:
        """Stop the daemon(s) and their sockets. Running QEMU processes are left alone."""
        if not self.manageable():
            raise DaemonError(400, f"Stopping libvirt is only supported for qemu:///system with systemd "
                                   f"(LIBVIRT_URI is {libvirt_client.uri})")
        if not self._action_lock.acquire(blocking=False):
            raise DaemonError(409, "libvirt is already being started or stopped")
        try:
            self._set_transition("stopping")
            libvirt_client.disconnect()
            units = self._units()
            # Everything libvirt-related that is up, in both layouts; sockets too, or the next
            # connection would start the daemon again
            todo = [n for n, props in units.items()
                    if props.get("LoadState") == "loaded" and props.get("ActiveState") not in ("inactive", "failed")]
            logger.info(f"Stopping libvirt: {' '.join(todo) or 'nothing to stop'}")
            self._systemctl_run("stop", todo)
        finally:
            self._set_transition(None)
            self._action_lock.release()
        return self.status()

    def shutdown_vms_and_stop(self, db, task, timeout: int) -> Dict[str, Any]:
        """Task body: ACPI-shutdown every running VM, wait for them, then stop libvirt"""
        from app.services.task_service import task_service

        names = self.running_vms()
        for name in names:
            try:
                libvirt_client.stop_vm(name)
            except libvirt.libvirtError as e:
                logger.warning(f"Could not shut down {name}: {e}")
        deadline = time.monotonic() + timeout
        remaining = names
        while remaining:
            if task_service.is_cancelled(task.id):
                raise RuntimeError("Cancelled")
            if time.monotonic() > deadline:
                raise RuntimeError(f"Timed out after {timeout}s, still running: {', '.join(remaining)}. "
                                   f"libvirt was not stopped.")
            time.sleep(2)
            running = set(self.running_vms())
            remaining = [n for n in names if n in running]
            if names:
                task_service.update_progress(db, task.id, int(90 * (len(names) - len(remaining)) / len(names)))
        self.stop()
        return {"shut_down": names}


daemon_service = DaemonService()
