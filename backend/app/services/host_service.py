"""Host Service"""
import logging
import os
import platform
import subprocess
from typing import Dict, Any, Optional

import psutil
from sqlalchemy.orm import Session

from app.config import settings
from app.libvirt_client import libvirt_client
from app.services.network_service import network_service
from app.services.storage_service import storage_service
from app.services.vm_service import vm_service

logger = logging.getLogger(__name__)


def _format_version(version: int) -> str:
    """libvirt encodes versions as major * 1000000 + minor * 1000 + micro"""
    return f"{version // 1000000}.{(version % 1000000) // 1000}.{version % 1000}" if version else ""


class HostService:
    """Host/node information"""

    def get_host_info(self, db: Session) -> Dict[str, Any]:
        lv = libvirt_client.get_host_info()

        vms = vm_service.list_vms(db)
        pools = storage_service.list_pools(db)
        volumes = storage_service.list_volumes(db)
        networks = network_service.list_networks(db)

        return {
            "hostname": lv["hostname"],
            "arch": lv["arch"],
            "cpu_model": lv["cpu_model"],
            "cpus": lv["cpus"],
            "sockets": lv["sockets"],
            "cores": lv["cores"],
            "threads": lv["threads"],
            "mhz": lv["mhz"],
            "memory": lv["memory"] * 1024 * 1024,
            "os_type": platform.system(),
            "os_version": self._os_pretty_name(),
            "kernel_version": platform.release(),
            "libvirt_uri": libvirt_client.uri,
            "libvirt_version": _format_version(lv["libvirt_version"]),
            "qemu_version": _format_version(lv["hypervisor_version"]) or None,
            "kvm_available": os.access("/dev/kvm", os.R_OK | os.W_OK),
            "emulator_available": lv["emulator_available"],
            "issues": self._issues(lv, pools, networks),
            "resources": self.get_resources(),
            "total_vms": len(vms),
            "running_vms": sum(1 for vm in vms if vm.status == "running"),
            "stopped_vms": sum(1 for vm in vms if vm.status in ("shutoff", "shutdown", "crashed")),
            "paused_vms": sum(1 for vm in vms if vm.status in ("paused", "pmsuspended")),
            "total_pools": len(pools),
            "total_volumes": len(volumes),
            "total_networks": len(networks),
            "active_networks": sum(1 for n in networks if n.active),
        }

    def _issues(self, lv: Dict[str, Any], pools, networks) -> list[str]:
        """Host setup problems that prevent VMs from being created or started"""
        issues = []
        if not lv["emulator_available"]:
            issues.append("libvirt reports no QEMU emulator: install QEMU on the host "
                          "(e.g. `sudo pacman -S qemu-base` or `qemu-full`) and restart libvirtd.")
        if not os.path.exists("/dev/kvm"):
            issues.append("/dev/kvm is missing: enable virtualization (VT-x/AMD-V) in the BIOS.")
        default_net = next((n for n in networks if n.name == settings.DEFAULT_NETWORK), None)
        if default_net is None:
            issues.append(f"Network '{settings.DEFAULT_NETWORK}' does not exist; new VMs attach to it.")
        elif not default_net.active:
            issues.append(f"Network '{settings.DEFAULT_NETWORK}' is inactive; start it (and enable autostart) "
                          f"on the Networks page, or VMs using it won't boot.")
        return issues

    def get_resources(self) -> Dict[str, Any]:
        memory = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage(settings.DEFAULT_POOL_PATH)
        except OSError:
            disk = psutil.disk_usage("/")
        return {
            "cpu_count": psutil.cpu_count(),
            "cpu_usage_percent": psutil.cpu_percent(interval=0.2),
            "memory_total": memory.total,
            "memory_used": memory.total - memory.available,
            "memory_free": memory.available,
            "memory_usage_percent": memory.percent,
            "disk_total": disk.total,
            "disk_used": disk.used,
            "disk_free": disk.free,
            "disk_usage_percent": disk.percent,
        }

    def _os_pretty_name(self) -> str:
        # platform.freedesktop_os_release() needs Python 3.10 (RHEL 9 has 3.9)
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return ""

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "xml": libvirt_client.get_capabilities_xml(),
            "arch": platform.machine(),
            "kvm_enabled": os.access("/dev/kvm", os.R_OK | os.W_OK),
        }

    def get_system_logs(self, lines: int = 100) -> str:
        try:
            result = subprocess.run(
                ["journalctl", "-n", str(lines), "--no-pager", "-u", "libvirtd", "-u", "virtqemud"],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            return "Unable to retrieve logs"


host_service = HostService()
