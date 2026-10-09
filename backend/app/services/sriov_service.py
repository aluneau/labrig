"""Host SR-IOV: physical functions (PFs) and their virtual functions (VFs), IOMMU readiness

Read from sysfs / procfs (the app runs on the libvirt host) and `ip -j -d link` (VF MAC / VLAN / trust /
spoofchk, unprivileged). Changes need root and go through the privileged helper: `sriov-set-numvfs`,
`sriov-vf-options` (trust / spoofchk on every VF of a PF) and `sriov-persist` (VF count + options kept in
/etc/vm-manager/sriov.conf, restored at boot by vm-manager-sriov.service). VFs are handed to VMs by libvirt
through "SR-IOV VF pool" networks (<forward mode='hostdev' managed='yes'><pf dev='...'/></forward>).

Every check returns plain-words text plus the fix (exact command for this distro when there is one).
"""
import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.helper_service import HelperError, run_helper

logger = logging.getLogger(__name__)

IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SRIOV_CONF = "/etc/vm-manager/sriov.conf"
SRIOV_UNIT = "vm-manager-sriov.service"
HELPER_MIN_VERSION = 3  # sriov-vf-options / sriov-persist / sriov-restore

# PF driver -> (VF driver, notes for the Host page)
DRIVERS = {
    "ixgbe": ("ixgbevf", "Intel 82599 / X520 / X540 / X550 (10 GbE), up to 63 VFs"),
    "i40e": ("iavf", "Intel X710 / XL710 / XXV710 (700 series), up to 128 VFs per port"),
    "ice": ("iavf", "Intel E810 (800 series), up to 256 VFs per port; needs the DDP package "
                    "(/lib/firmware/intel/ice/ddp), else the PF runs in safe mode without SR-IOV"),
    "igb": ("igbvf", "Intel 82576 / I350 (1 GbE), up to 7 VFs (also QEMU's emulated igb)"),
    "mlx5_core": ("mlx5_core", "NVIDIA/Mellanox ConnectX-4/5/6/7, the firmware caps the VF count (mstconfig "
                               "SRIOV_EN / NUM_OF_VFS); VF pools need the eswitch in legacy mode (the default), "
                               "not switchdev"),
    "bnxt_en": ("bnxt_en", "Broadcom NetXtreme-C/E"),
    "qede": ("qede", "Marvell/QLogic FastLinQ"),
    "sfc": ("sfc", "AMD/Solarflare"),
}

FW_HINTS = {
    "mlx5_core": "The firmware has SR-IOV off or 0 VFs: install mstflint, then "
                 "`sudo mstconfig -d {pci} set SRIOV_EN=1 NUM_OF_VFS=8` and reboot (or `mstfwreset -d {pci} reset`).",
    "i40e": "The NVM may limit VFs, or SR-IOV is off in the server firmware (BIOS 'SR-IOV Global Enable' / the "
            "NIC's HII menu). Update the NVM with Intel's nvmupdate tool if `ethtool -i {iface}` shows an old one.",
    "ice": "Check that the DDP package loaded (`dmesg | grep -i ddp`: 'safe mode' = no SR-IOV) and that SR-IOV is "
           "enabled in the server firmware.",
    "ixgbe": "Enable SR-IOV in the server firmware (BIOS 'SR-IOV Global Enable').",
}


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


def _check(id: str, label: str, status: str, detail: str, fix: Optional[str] = None) -> Dict[str, Any]:
    """status: ok | warning | error | info"""
    return {"id": id, "label": label, "status": status, "detail": detail, "fix": fix}


class SriovService:
    # Prefix for /sys, /proc and /etc (tests point it at a fake tree)
    root = ""

    def _p(self, path: str) -> str:
        return self.root + path

    # Host-level facts

    def _os_release(self) -> Dict[str, str]:
        info = {}
        for line in (_read(self._p("/etc/os-release")) or "").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                info[key] = value.strip().strip('"')
        return info

    def _cpu(self) -> Dict[str, Any]:
        text = _read(self._p("/proc/cpuinfo")) or ""
        vendor = re.search(r"^vendor_id\s*:\s*(\S+)", text, re.M)
        flags = re.search(r"^flags\s*:\s*(.*)$", text, re.M)
        return {"vendor": "amd" if vendor and vendor.group(1) == "AuthenticAMD" else "intel",
                "vm": bool(flags and "hypervisor" in flags.group(1).split())}

    def _iommu_groups(self) -> int:
        try:
            return len(os.listdir(self._p("/sys/kernel/iommu_groups")))
        except OSError:
            return 0

    def kernel_args_fix(self, args: str) -> str:
        """Exact command(s) adding kernel arguments on this distro / boot loader"""
        osr = self._os_release()
        ids = {osr.get("ID", "")} | set(osr.get("ID_LIKE", "").split())
        if ids & {"rhel", "fedora", "centos"}:
            return f'sudo grubby --update-kernel=ALL --args="{args}" && sudo reboot'
        if "pop" in ids or os.path.exists(self._p("/usr/bin/kernelstub")):
            return "sudo kernelstub " + " ".join(f"-a {a}" for a in args.split()) + " && sudo reboot"
        if os.path.isfile(self._p("/etc/default/limine")):
            return (f"Add `{args}` to KERNEL_CMDLINE[default] in /etc/default/limine, run `sudo limine-update`, "
                    f"then reboot")
        if os.path.isfile(self._p("/etc/kernel/cmdline")):
            return (f"Add `{args}` to /etc/kernel/cmdline, regenerate the boot images (`sudo kernel-install add-all`, "
                    f"or `sudo mkinitcpio -P` on Arch with UKIs), then reboot")
        if ids & {"debian", "ubuntu"}:
            return (f"sudo sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=\"/&{args} /' /etc/default/grub && "
                    f"sudo update-grub && sudo reboot")
        if ids & {"suse", "opensuse"}:
            return (f"sudo sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=\"/&{args} /' /etc/default/grub && "
                    f"sudo grub2-mkconfig -o /boot/grub2/grub.cfg && sudo reboot")
        if os.path.isdir(self._p("/boot/loader/entries")):
            return f"Add `{args}` to the `options` line of /boot/loader/entries/*.conf (systemd-boot), then reboot"
        return (f"sudo sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=\"/&{args} /' /etc/default/grub && "
                f"sudo grub-mkconfig -o /boot/grub/grub.cfg && sudo reboot")

    def iommu(self) -> Dict[str, Any]:
        """{"enabled", "groups", "message"}: enabled = the kernel created IOMMU groups (vfio usable)"""
        groups = self._iommu_groups()
        message = None
        if not groups:
            failing = [c for c in self._iommu_checks() if c["status"] == "error"]
            message = " ".join(f"{c['detail']}" + (f" Fix: {c['fix']}" if c["fix"] else "") for c in failing) or (
                "The host has no active IOMMU, so VFs can't be passed through to VMs (vfio).")
        return {"enabled": groups > 0, "groups": groups, "message": message}

    def _iommu_checks(self) -> List[Dict[str, Any]]:
        cpu = self._cpu()
        groups = self._iommu_groups()
        cmdline = (_read(self._p("/proc/cmdline")) or "").split()
        intel = cpu["vendor"] == "intel"
        checks = []

        # 1. Firmware: the ACPI table describing the IOMMU (DMAR = Intel VT-d, IVRS = AMD-Vi)
        table = "DMAR" if intel else "IVRS"
        name = "VT-d" if intel else "AMD-Vi (IOMMU)"
        if os.path.exists(self._p(f"/sys/firmware/acpi/tables/{table}")) or groups:
            checks.append(_check("firmware", f"{name} in the firmware", "ok",
                                 f"The firmware exposes the IOMMU (ACPI {table} table)."))
        elif cpu["vm"]:
            checks.append(_check("firmware", f"{name} in the firmware", "error",
                                 "This host is a VM without a virtual IOMMU (no ACPI DMAR table).",
                                 "Give the VM a virtual IOMMU (VM Manager: Devices → Virtual IOMMU, then a cold "
                                 "start; libvirt: <iommu model='intel'> + <ioapic driver='qemu'/>)."))
        else:
            checks.append(_check("firmware", f"{name} in the firmware", "error",
                                 f"The firmware doesn't expose an IOMMU (no ACPI {table} table): {name} is "
                                 f"disabled in the BIOS/UEFI setup.",
                                 f"Reboot into the BIOS/UEFI setup and enable "
                                 + ("'Intel VT-d' / 'Intel Virtualization Technology for Directed I/O'"
                                    if intel else "'IOMMU' / 'AMD-Vi'")
                                 + " (often under Processor or Chipset settings), plus 'SR-IOV Global Enable' "
                                   "if the server has it."))

        # 2. Kernel: IOMMU groups exist only when the kernel turned the IOMMU on
        args = "intel_iommu=on iommu=pt" if intel else "iommu=pt"
        if groups:
            checks.append(_check("kernel", "IOMMU enabled by the kernel", "ok",
                                 f"{groups} IOMMU groups: devices can be given to vfio."))
        else:
            on_arg = "intel_iommu=on" in cmdline if intel else "amd_iommu=off" not in cmdline
            if checks[0]["status"] == "ok" and not on_arg:
                checks.append(_check("kernel", "IOMMU enabled by the kernel", "error",
                                     "The kernel didn't turn the IOMMU on: "
                                     + ("intel_iommu=on is missing from the kernel command line."
                                        if intel else "amd_iommu=off is on the kernel command line."),
                                     self.kernel_args_fix(args) if intel else
                                     "Remove amd_iommu=off from the kernel command line and reboot."))
            elif checks[0]["status"] == "ok":
                checks.append(_check("kernel", "IOMMU enabled by the kernel", "error",
                                     "The kernel has the IOMMU option but created no IOMMU groups: check "
                                     "`dmesg | grep -iE 'DMAR|IOMMU'` (firmware bug, or the IOMMU is disabled "
                                     "for this platform)."))
            else:
                checks.append(_check("kernel", "IOMMU enabled by the kernel", "error",
                                     "No IOMMU groups (the firmware IOMMU is off, see above)."
                                     + ("" if "intel_iommu=on" in cmdline or not intel
                                        else " Also add intel_iommu=on to the kernel command line."),
                                     None if "intel_iommu=on" in cmdline or not intel else self.kernel_args_fix(args)))

        # 3. iommu=pt: recommended (host devices keep identity mapping, only vfio devices are translated)
        if groups and "iommu=pt" not in cmdline:
            checks.append(_check("passthrough", "iommu=pt", "info",
                                 "Optional, recommended by Red Hat for SR-IOV / DPDK hosts: iommu=pt keeps the "
                                 "host's own devices out of IOMMU translation (no I/O overhead for them).",
                                 self.kernel_args_fix("iommu=pt")))
        elif groups:
            checks.append(_check("passthrough", "iommu=pt", "ok", "iommu=pt is on the kernel command line."))

        # 4. Interrupt remapping (Intel: ecap bit 3): without it vfio refuses devices unless told it's unsafe
        if intel and groups:
            ecaps = []
            try:
                for unit in os.listdir(self._p("/sys/class/iommu")):
                    value = _read(self._p(f"/sys/class/iommu/{unit}/intel-iommu/ecap"))
                    if value:
                        ecaps.append(int(value, 16))
            except (OSError, ValueError):
                pass
            if ecaps and not all(e & 0x8 for e in ecaps):
                checks.append(_check("intremap", "Interrupt remapping", "warning",
                                     "The IOMMU doesn't support interrupt remapping: vfio refuses to assign "
                                     "devices unless allowed (unsafe).",
                                     "Update the BIOS / enable 'x2APIC' and 'Interrupt Remapping' in the firmware; "
                                     "last resort: options vfio_iommu_type1 allow_unsafe_interrupts=1"))
            elif ecaps:
                checks.append(_check("intremap", "Interrupt remapping", "ok",
                                     "The IOMMU supports interrupt remapping."))
        return checks

    def _vfio_check(self) -> Dict[str, Any]:
        if os.path.isdir(self._p("/sys/module/vfio_pci")):
            return _check("vfio", "vfio-pci driver", "ok", "vfio-pci is loaded.")
        release = os.uname().release
        moddir = self._p(f"/lib/modules/{release}")
        text = (_read(os.path.join(moddir, "modules.dep")) or "") + (_read(os.path.join(moddir, "modules.builtin")) or "")
        if re.search(r"vfio[-_]pci\.ko", text):
            return _check("vfio", "vfio-pci driver", "ok",
                          "vfio-pci is available (libvirt loads it when a VM gets a VF).")
        return _check("vfio", "vfio-pci driver", "error",
                      f"vfio-pci isn't available for kernel {release}: VFs can't be passed through.",
                      "EL: sudo dnf install kernel-modules-core (vfio-pci ships there); Debian: it's in the stock "
                      "linux-image package. Then reboot into that kernel if it was just updated.")

    def helper_version(self) -> Optional[int]:
        """Version of the installed privileged helper (read from the file: no pkexec), None if missing"""
        text = _read(settings.HELPER_PATH)
        if text is None:
            return None
        m = re.search(r'^VERSION = "(\d+)"', text, re.M)
        return int(m.group(1)) if m else 0

    def _helper_check(self) -> Dict[str, Any]:
        version = self.helper_version()
        if version is None:
            return _check("helper", "Privileged helper", "error",
                          f"{settings.HELPER_PATH} isn't installed: VF counts can't be changed from the app.",
                          "Run scripts/setup.sh on this host.")
        if version < HELPER_MIN_VERSION:
            return _check("helper", "Privileged helper", "warning",
                          f"The installed helper is version {version}: VF options and persistence need version "
                          f"{HELPER_MIN_VERSION}.", "Re-run scripts/setup.sh on this host.")
        return _check("helper", "Privileged helper", "ok", f"Version {version}.")

    def unit_enabled(self) -> Optional[bool]:
        systemctl = shutil.which("systemctl", path=SYSTEM_PATH)
        if systemctl is None or self.root:
            return None
        try:
            result = subprocess.run([systemctl, "is-enabled", SRIOV_UNIT], capture_output=True, text=True,
                                    timeout=10, env={"PATH": SYSTEM_PATH, "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() == "enabled"

    def stored_config(self) -> Dict[str, Dict[str, Any]]:
        """/etc/vm-manager/sriov.conf (written by the helper, world-readable): PCI address -> entry"""
        try:
            with open(self._p(SRIOV_CONF)) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        pfs = data.get("pfs") if isinstance(data, dict) else None
        return pfs if isinstance(pfs, dict) else {}

    # PFs and VFs

    def _ip_vfs(self, pf: str) -> Dict[int, Dict[str, Any]]:
        """VF settings as the PF driver reports them (`ip -j -d link show dev <pf>`)"""
        ip = shutil.which("ip", path=SYSTEM_PATH)
        if ip is None or self.root:
            return {}
        try:
            result = subprocess.run([ip, "-j", "-d", "link", "show", "dev", pf], capture_output=True, text=True,
                                    timeout=10, env={"PATH": SYSTEM_PATH, "LC_ALL": "C"})
            data = json.loads(result.stdout or "[]")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {}
        out = {}
        for vf in (data[0].get("vfinfo_list") or []) if data else []:
            if "vf" not in vf:
                continue
            vlan = None
            if vf.get("vlan_list"):
                vlan = vf["vlan_list"][0].get("vlan")
            elif vf.get("vlan"):
                vlan = vf.get("vlan")
            out[int(vf["vf"])] = {
                "mac": vf.get("address") or vf.get("mac"),
                "vlan": vlan or None,
                "spoofchk": vf.get("spoofchk"),
                "trust": vf.get("trust"),
                "link_state": vf.get("link_state"),
            }
        return out

    def _group(self, pci_dir: str) -> Dict[str, Any]:
        """IOMMU group of a PCI device and the *other* endpoint devices in it (bridges don't count)"""
        group = _link_name(os.path.join(pci_dir, "iommu_group"))
        others = []
        if group is not None:
            try:
                members = sorted(os.listdir(self._p(f"/sys/kernel/iommu_groups/{group}/devices")))
            except OSError:
                members = []
            me = os.path.basename(os.path.realpath(pci_dir))
            for dev in members:
                if dev == me:
                    continue
                cls = _read(self._p(f"/sys/bus/pci/devices/{dev}/class")) or ""
                if cls.startswith("0x0604"):  # PCI bridge / root port: fine for vfio
                    continue
                others.append(dev)
        return {"iommu_group": int(group) if group and group.isdigit() else None, "group_others": others}

    def _vfs(self, device_dir: str, ip_vfs: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
        vfs = []
        try:
            entries = sorted((e for e in os.listdir(device_dir) if e.startswith("virtfn")),
                             key=lambda e: int(e[len("virtfn"):]))
        except OSError:
            return []
        for entry in entries:
            vf_dir = os.path.join(device_dir, entry)
            index = int(entry[len("virtfn"):])
            try:
                netdevs = os.listdir(os.path.join(vf_dir, "net"))
            except OSError:
                netdevs = []
            driver = _link_name(os.path.join(vf_dir, "driver"))  # igbvf, iavf, mlx5_core, vfio-pci, None
            vfs.append({
                "index": index,
                "pci": _link_name(vf_dir),
                "driver": driver,
                "netdev": netdevs[0] if netdevs else None,
                "in_use": driver == "vfio-pci",  # bound to vfio-pci = passed through (libvirt managed)
                **self._group(vf_dir),
                **{k: None for k in ("mac", "vlan", "spoofchk", "trust", "link_state")},
                **ip_vfs.get(index, {}),
            })
        return vfs

    def list_pfs(self, details: bool = True) -> List[Dict[str, Any]]:
        """Network interfaces whose PCI device has SR-IOV (a sriov_totalvfs file, even if the firmware says 0)"""
        sys_net = self._p("/sys/class/net")
        pfs = []
        try:
            names = sorted(os.listdir(sys_net))
        except OSError:
            return []
        stored = self.stored_config() if details else {}
        for name in names:
            device_dir = os.path.join(sys_net, name, "device")
            total = _read(os.path.join(device_dir, "sriov_totalvfs"))
            if total is None or not total.isdigit():
                continue
            num = _read(os.path.join(device_dir, "sriov_numvfs"))
            pci = _link_name(device_dir)
            entry = stored.get(pci) or {}
            pf = {
                "name": name,
                "pci": pci,
                "driver": _link_name(os.path.join(device_dir, "driver")),
                "vendor_id": (_read(os.path.join(device_dir, "vendor")) or "").replace("0x", "") or None,
                "device_id": (_read(os.path.join(device_dir, "device")) or "").replace("0x", "") or None,
                "vf_device_id": (_read(os.path.join(device_dir, "sriov_vf_device")) or "") or None,
                "total_vfs": int(total),
                "num_vfs": int(num) if num and num.isdigit() else 0,
                "operstate": _read(os.path.join(sys_net, name, "operstate")),
                "carrier": _read(os.path.join(sys_net, name, "carrier")) == "1",
                "persistent": entry.get("persistent") is True,
                "persisted_num_vfs": entry.get("num_vfs") if entry.get("persistent") else None,
                "trust": entry.get("trust"),
                "spoofchk": entry.get("spoofchk"),
                "vfs": self._vfs(device_dir, self._ip_vfs(name) if details else {}),
            }
            if details:
                pf["checks"] = self._pf_checks(pf)
            pfs.append(pf)
        return pfs

    def _pf_checks(self, pf: Dict[str, Any]) -> List[Dict[str, Any]]:
        checks = []
        driver = pf["driver"] or ""
        known = DRIVERS.get(driver)
        if not driver:
            checks.append(_check("driver", "PF driver", "error", "No driver is bound to the PF.",
                                 "Load the NIC's driver (modprobe ixgbe / i40e / ice / mlx5_core …)."))
        else:
            checks.append(_check("driver", "PF driver", "ok" if known else "info",
                                 f"{driver}: {known[1]}; VFs use {known[0]}." if known
                                 else f"{driver}: not a driver this app was tested with; SR-IOV may still work."))
        fmt = {"pci": pf["pci"], "iface": pf["name"]}
        if pf["total_vfs"] == 0:
            checks.append(_check("firmware_vfs", "VFs allowed by the firmware", "error",
                                 "The NIC reports 0 possible VFs (sriov_totalvfs = 0).",
                                 FW_HINTS.get(driver, "Enable SR-IOV in the server firmware / NIC firmware.").format(**fmt)))
        else:
            checks.append(_check("firmware_vfs", "VFs allowed by the firmware", "ok",
                                 f"Up to {pf['total_vfs']} VFs (sriov_totalvfs)."
                                 + (f" The NIC firmware sets this limit: raise it with `sudo mstconfig -d "
                                    f"{pf['pci']} set NUM_OF_VFS=<n>` and a reboot" if driver == "mlx5_core" else "")))
        try:
            admin_up = bool(int(_read(self._p(f"/sys/class/net/{pf['name']}/flags")) or "0", 16) & 1)
        except ValueError:
            admin_up = pf["operstate"] == "up"
        if admin_up and pf["carrier"]:
            checks.append(_check("link", "PF link", "ok", "The PF is up with a carrier: VFs reach the wire."))
        elif admin_up:
            checks.append(_check("link", "PF link", "warning",
                                 "The PF is up but has no carrier: VFs pass no traffic (cable, transceiver, switch "
                                 "port shut down?).", f"Check `ethtool {pf['name']}` and the switch port."))
        else:
            checks.append(_check("link", "PF link", "warning",
                                 "The PF is administratively down: some drivers (igb, i40e) keep VFs in reset or "
                                 "without link while the PF is down.",
                                 f"sudo ip link set {pf['name']} up   (persist it: nmcli connection add type "
                                 f"ethernet ifname {pf['name']} con-name {pf['name']} ipv4.method disabled "
                                 f"ipv6.method ignore)"))
        if pf["num_vfs"] and self._iommu_groups():
            shared = [vf for vf in pf["vfs"] if vf["group_others"]]
            if shared:
                vf = shared[0]
                checks.append(_check("isolation", "IOMMU isolation of the VFs", "warning",
                                     f"{len(shared)} of {len(pf['vfs'])} VFs share their IOMMU group with other "
                                     f"devices (e.g. VF {vf['pci']}: group {vf['iommu_group']} also has "
                                     f"{', '.join(vf['group_others'])}). vfio can only take a whole group, so "
                                     f"those VFs can't be passed through.",
                                     "The slot / PF lacks ACS isolation: use a NIC or slot with ACS (CPU root "
                                     "ports usually have it), or enable ACS / 'PCIe ARI' in the firmware."))
            else:
                checks.append(_check("isolation", "IOMMU isolation of the VFs", "ok",
                                     "Every VF is alone in its IOMMU group."))
        if pf["persistent"]:
            if pf["persisted_num_vfs"] != pf["num_vfs"]:
                checks.append(_check("persistent", "Kept across reboots", "warning",
                                     f"{pf['persisted_num_vfs']} VFs are restored at boot, {pf['num_vfs']} now.",
                                     "Turn persistence off and on to store the current count."))
            else:
                checks.append(_check("persistent", "Kept across reboots", "ok",
                                     f"{pf['num_vfs']} VFs (and VF options) are restored at boot by {SRIOV_UNIT}."))
        elif pf["num_vfs"]:
            checks.append(_check("persistent", "Kept across reboots", "info",
                                 "The VF count is lost when the host reboots.",
                                 "Turn on 'Keep across reboots' (installs vm-manager-sriov.service)."))
        for key, want in (("trust", pf["trust"]), ("spoofchk", pf["spoofchk"])):
            if want is None:
                continue
            drift = [vf["index"] for vf in pf["vfs"] if vf.get(key) is not None and vf[key] != want]
            if drift:
                checks.append(_check(f"vf_{key}", f"VF {key}", "warning",
                                     f"VFs {', '.join(map(str, drift))} have {key} {'off' if want else 'on'} "
                                     f"(wanted {'on' if want else 'off'}): the driver reset them?",
                                     "Apply the VF options again."))
        return checks

    def get_pf(self, name: str, details: bool = True) -> Optional[Dict[str, Any]]:
        return next((pf for pf in self.list_pfs(details) if pf["name"] == name), None)

    def status(self) -> Dict[str, Any]:
        checks = self._iommu_checks() + [self._vfio_check(), self._helper_check()]
        pfs = self.list_pfs()
        if any(pf["persistent"] for pf in pfs):
            enabled = self.unit_enabled()
            if enabled is False:
                checks.append(_check("unit", SRIOV_UNIT, "warning",
                                     "PFs are marked persistent but the unit that restores them at boot isn't enabled.",
                                     f"sudo systemctl enable {SRIOV_UNIT}"))
            elif enabled:
                checks.append(_check("unit", SRIOV_UNIT, "ok", "Enabled: restores VF counts and options at boot."))
        return {"iommu": self.iommu(), "checks": checks, "pfs": pfs, "helper_version": self.helper_version()}

    # Changes (privileged helper)

    def _need_helper(self, what: str) -> None:
        version = self.helper_version()
        if version is not None and version < HELPER_MIN_VERSION:
            raise HelperError(501, f"{what} needs a newer privileged helper (installed: version {version}): "
                                   f"re-run scripts/setup.sh on this host")

    def update_pf(self, name: str, num_vfs: Optional[int] = None, persistent: Optional[bool] = None,
                  trust: Optional[bool] = None, spoofchk: Optional[bool] = None) -> Dict[str, Any]:
        """VF count, VF options (trust / spoofchk on every VF) and persistence across reboots of a PF"""
        if not IFACE_RE.match(name):
            raise ValueError(f"Invalid interface name '{name}'")
        pf = self.get_pf(name, details=False)
        if pf is None:
            raise LookupError(f"{name} is not an SR-IOV capable interface (no sriov_totalvfs)")
        if num_vfs is not None:
            if pf["total_vfs"] == 0:
                raise ValueError(f"{name}'s firmware allows no VFs (sriov_totalvfs = 0): see the Host page checks")
            if not 0 <= num_vfs <= pf["total_vfs"]:
                raise ValueError(f"{name} supports 0 to {pf['total_vfs']} VFs")
            if num_vfs != pf["num_vfs"]:
                busy = [vf["pci"] for vf in pf["vfs"] if vf["in_use"]]
                if busy:
                    raise ValueError(f"Can't change the VF count of {name}: "
                                     + (f"VF {busy[0]} is" if len(busy) == 1 else f"VFs {', '.join(busy)} are")
                                     + " passed through to running VMs (stop the VMs using its pools first)")
                run_helper(["sriov-set-numvfs", name, str(num_vfs)], timeout=120)
                logger.info(f"Set {num_vfs} VFs on {name}")
        if trust is not None or spoofchk is not None:
            self._need_helper("VF options")
            stored = self.stored_config().get(pf["pci"]) or {}
            t = trust if trust is not None else stored.get("trust", False)
            s = spoofchk if spoofchk is not None else stored.get("spoofchk", True)
            run_helper(["sriov-vf-options", name, "trust", "on" if t else "off", "spoofchk", "on" if s else "off"],
                       timeout=120)
        if persistent is not None:
            self._need_helper("Keeping VFs across reboots")
            run_helper(["sriov-persist", name, "on" if persistent else "off"], timeout=120)
        return self.get_pf(name)

    def set_num_vfs(self, name: str, num_vfs: int) -> Dict[str, Any]:
        return self.update_pf(name, num_vfs=num_vfs)

    # VF pools (hostdev networks): plain-words pre-flight checks before libvirt picks a VF

    def check_pool(self, network: str, pf_name: Optional[str], needed: int = 1) -> None:
        """Raise ValueError with the reason when `needed` VFs can't come from this pool right now"""
        iommu = self.iommu()
        if not iommu["enabled"]:
            raise ValueError(f"'{network}' is an SR-IOV VF pool, but this host has no active IOMMU, so VFs can't "
                             f"be passed through. {iommu['message']}")
        if not pf_name:
            return  # a pool defined by a list of VFs in its XML: libvirt checks it
        pf = self.get_pf(pf_name)
        if pf is None:
            raise ValueError(f"The PF '{pf_name}' of VF pool '{network}' is not an SR-IOV interface on this host any "
                             f"more (renamed or removed NIC?): recreate the pool with the right PF")
        if pf["num_vfs"] == 0:
            raise ValueError(f"No VF in pool '{network}': {pf_name} has 0 VFs. Set a VF count on the Host page "
                             f"(SR-IOV) first")
        free = [vf for vf in pf["vfs"] if not vf["in_use"]]
        if len(free) < needed:
            used = len(pf["vfs"]) - len(free)
            if not free:
                what = f"No free VF in pool '{network}': all {len(pf['vfs'])} VFs of {pf_name} are"
            else:
                what = (f"Not enough free VFs in pool '{network}': this needs {needed}, {pf_name} has "
                        f"{len(pf['vfs'])} VFs and {used}" + (" is" if used == 1 else " are"))
            raise ValueError(what + " already passed through to running VMs. Raise the VF count on the Host "
                                    "page (SR-IOV), or stop a VM using the pool"
                             if used else
                             f"Not enough VFs in pool '{network}': this needs {needed}, {pf_name} has only "
                             f"{len(pf['vfs'])}. Raise the VF count on the Host page (SR-IOV)")
        usable = [vf for vf in free if not vf["group_others"]]
        if len(usable) < needed:
            vf = next(vf for vf in free if vf["group_others"])
            raise ValueError(f"The VFs of {pf_name} can't be passed through: VF {vf['pci']}'s IOMMU group "
                             f"{vf['iommu_group']} also contains {', '.join(vf['group_others'])}, and vfio can only "
                             f"take a whole group (no ACS isolation on this slot). Use a NIC/slot with ACS")


sriov_service = SriovService()
