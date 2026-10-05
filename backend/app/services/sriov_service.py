"""Host SR-IOV: physical functions (PFs) and their virtual functions (VFs), IOMMU state

Read from sysfs: the app runs on the libvirt host. Changing the number of VFs needs root and goes
through the privileged helper (`sriov-set-numvfs`). VFs are then handed to VMs by libvirt through
"SR-IOV VF pool" networks (<forward mode='hostdev' managed='yes'><pf dev='...'/></forward>).
"""
import logging
import os
import re
from typing import Any, Dict, List, Optional

from app.services.helper_service import run_helper

logger = logging.getLogger(__name__)

SYS_NET = "/sys/class/net"
IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")

NO_IOMMU_HINT = (
    "The host has no active IOMMU, so VFs can't be passed through to VMs (vfio). Enable VT-d / AMD-Vi in "
    "the firmware and add intel_iommu=on (Intel; AMD enables it by default) iommu=pt to the kernel command "
    "line, then reboot. VFs still work as host network interfaces without it.")


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _link_name(path: str) -> Optional[str]:
    try:
        return os.path.basename(os.readlink(path))
    except OSError:
        return None


class SriovService:

    def iommu(self) -> Dict[str, Any]:
        """{"enabled", "groups", "message"}: enabled = the kernel created IOMMU groups (vfio usable)"""
        try:
            groups = len(os.listdir("/sys/kernel/iommu_groups"))
        except OSError:
            groups = 0
        cmdline = _read("/proc/cmdline") or ""
        message = None
        if not groups:
            message = NO_IOMMU_HINT
            if "intel_iommu=on" in cmdline or "amd_iommu=on" in cmdline:
                message = ("The kernel was started with an IOMMU option but no IOMMU groups exist: VT-d / AMD-Vi "
                           "is probably disabled in the firmware (or, in a VM, the VM has no virtual IOMMU).")
        return {"enabled": groups > 0, "groups": groups, "message": message}

    def _vfs(self, device_dir: str) -> List[Dict[str, Any]]:
        vfs = []
        try:
            entries = sorted((e for e in os.listdir(device_dir) if e.startswith("virtfn")),
                             key=lambda e: int(e[len("virtfn"):]))
        except OSError:
            return []
        for entry in entries:
            vf_dir = os.path.join(device_dir, entry)
            try:
                netdevs = os.listdir(os.path.join(vf_dir, "net"))
            except OSError:
                netdevs = []
            vfs.append({
                "index": int(entry[len("virtfn"):]),
                "pci": _link_name(vf_dir),
                "driver": _link_name(os.path.join(vf_dir, "driver")),  # igbvf, iavf, vfio-pci, None
                "netdev": netdevs[0] if netdevs else None,
            })
        return vfs

    def list_pfs(self) -> List[Dict[str, Any]]:
        """Network interfaces whose PCI device supports SR-IOV (sriov_totalvfs > 0)"""
        pfs = []
        try:
            names = sorted(os.listdir(SYS_NET))
        except OSError:
            return []
        for name in names:
            device_dir = os.path.join(SYS_NET, name, "device")
            total = _read(os.path.join(device_dir, "sriov_totalvfs"))
            if not total or not total.isdigit() or int(total) == 0:
                continue
            num = _read(os.path.join(device_dir, "sriov_numvfs"))
            pfs.append({
                "name": name,
                "pci": _link_name(device_dir),
                "driver": _link_name(os.path.join(device_dir, "driver")),
                "vendor_id": (_read(os.path.join(device_dir, "vendor")) or "").replace("0x", "") or None,
                "device_id": (_read(os.path.join(device_dir, "device")) or "").replace("0x", "") or None,
                "vf_device_id": (_read(os.path.join(device_dir, "sriov_vf_device")) or "") or None,
                "total_vfs": int(total),
                "num_vfs": int(num) if num and num.isdigit() else 0,
                "operstate": _read(os.path.join(SYS_NET, name, "operstate")),
                "vfs": self._vfs(device_dir),
            })
        return pfs

    def get_pf(self, name: str) -> Optional[Dict[str, Any]]:
        return next((pf for pf in self.list_pfs() if pf["name"] == name), None)

    def status(self) -> Dict[str, Any]:
        return {"iommu": self.iommu(), "pfs": self.list_pfs()}

    def set_num_vfs(self, name: str, num_vfs: int) -> Dict[str, Any]:
        """Create / remove VFs on a PF (root, through the helper). Not persistent across host reboots."""
        if not IFACE_RE.match(name):
            raise ValueError(f"Invalid interface name '{name}'")
        pf = self.get_pf(name)
        if pf is None:
            raise LookupError(f"{name} is not an SR-IOV capable interface (no sriov_totalvfs)")
        if not 0 <= num_vfs <= pf["total_vfs"]:
            raise ValueError(f"{name} supports 0 to {pf['total_vfs']} VFs")
        if num_vfs != pf["num_vfs"]:
            run_helper(["sriov-set-numvfs", name, str(num_vfs)], timeout=60)
            logger.info(f"Set {num_vfs} VFs on {name}")
        return self.get_pf(name)


sriov_service = SriovService()
