"""Pure helpers on libvirt domain XML: disks, CD-ROMs, boot order (no libvirt calls)

Boot order has two mutually exclusive styles in libvirt:
- `<os><boot dev='hd'/><boot dev='cdrom'/></os>` (by device type: libvirt boots the *first*
  device of each type), and
- per-device `<boot order='N'/>` inside `<disk>` / `<interface>`.
We keep whichever style the XML uses, and switch to per-device only when "cdrom" must point
at a specific CD-ROM (cloud-image VMs have a cloud-init seed CD-ROM next to the user one).
"""
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional
from xml.sax.saxutils import quoteattr

BOOT_DEVICES = ("hd", "cdrom", "network")


def is_seed(path: Optional[str]) -> bool:
    """Our generated cloud-init NoCloud seed (never the user's CD-ROM)"""
    return bool(path) and path.endswith("-cidata.iso")


def _source(disk: ET.Element) -> Optional[str]:
    source = disk.find("source")
    if source is None:
        return None
    return source.get("file") or source.get("dev") or source.get("name")


def _target(disk: ET.Element) -> Optional[str]:
    target = disk.find("target")
    return target.get("dev") if target is not None else None


def disk_elements(root: ET.Element) -> List[ET.Element]:
    return root.findall("./devices/disk")


def find_disk(root: ET.Element, target: str) -> Optional[ET.Element]:
    for disk in disk_elements(root):
        if _target(disk) == target:
            return disk
    return None


def disk_info(disk: ET.Element) -> Dict[str, Any]:
    target = disk.find("target")
    driver = disk.find("driver")
    boot = disk.find("boot")
    alias = disk.find("alias")
    return {
        "device": disk.get("device"),
        "path": _source(disk),
        "target": _target(disk),
        "bus": target.get("bus") if target is not None else None,
        "format": driver.get("type") if driver is not None else None,
        "boot_order": int(boot.get("order")) if boot is not None and boot.get("order") else None,
        "alias": alias.get("name") if alias is not None else None,
    }


def user_cdrom(root: ET.Element) -> Optional[ET.Element]:
    """The CD-ROM the user controls: the first one (by target) not holding a cloud-init seed"""
    cdroms = [d for d in disk_elements(root) if d.get("device") == "cdrom" and not is_seed(_source(d))]
    return sorted(cdroms, key=lambda d: _target_key(_target(d) or ""))[0] if cdroms else None


def boot_disk(root: ET.Element) -> Optional[ET.Element]:
    """The disk the VM boots from: lowest per-device boot order, else the first disk by target"""
    disks = [d for d in disk_elements(root) if d.get("device") == "disk"]
    if not disks:
        return None
    ordered = [d for d in disks if d.find("boot") is not None]
    if ordered:
        return min(ordered, key=lambda d: int(d.find("boot").get("order", "999")))
    return sorted(disks, key=lambda d: _target_key(_target(d) or ""))[0]


# Target names: vda..vdz, vdaa.. (same scheme as libvirt)

def _suffix(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("a") + rem) + letters
    return letters


def _target_key(target: str):
    prefix, letters = target[:2], target[2:]
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("a") + 1)
    return (prefix, index)


def next_target(roots: Iterable[ET.Element], prefix: str) -> str:
    """First free <prefix>X target (prefix 'vd' or 'sd') across all given XML trees"""
    used = {_target(d) for root in roots for d in disk_elements(root)}
    i = 0
    while f"{prefix}{_suffix(i)}" in used:
        i += 1
    return f"{prefix}{_suffix(i)}"


# Device XML

def cdrom_xml(target: str, iso_path: Optional[str], bus: str = "sata") -> str:
    source = f"<source file={quoteattr(iso_path)}/>" if iso_path else ""
    return (f"<disk type='file' device='cdrom'><driver name='qemu' type='raw'/>{source}"
            f"<target dev={quoteattr(target)} bus={quoteattr(bus)}/><readonly/></disk>")


def media_change_xml(cdrom: ET.Element, iso_path: Optional[str]) -> str:
    """Copy of an existing CD-ROM element with new media (None = ejected), for updateDeviceFlags"""
    disk = ET.fromstring(ET.tostring(cdrom))
    disk.set("type", "file")
    for source in disk.findall("source"):
        disk.remove(source)
    if iso_path:
        source = ET.Element("source", {"file": iso_path})
        disk.insert(1 if disk.find("driver") is not None else 0, source)
    return ET.tostring(disk, encoding="unicode")


def data_disk_xml(path: str, target: str, bus: str = "virtio", fmt: str = "qcow2") -> str:
    discard = " discard='unmap'" if fmt == "qcow2" else ""
    return (f"<disk type='file' device='disk'><driver name='qemu' type={quoteattr(fmt)}{discard}/>"
            f"<source file={quoteattr(path)}/><target dev={quoteattr(target)} bus={quoteattr(bus)}/></disk>")


# Boot order

def _kind(element: ET.Element) -> Optional[str]:
    if element.tag == "interface":
        return "network"
    if element.tag == "disk":
        return {"disk": "hd", "cdrom": "cdrom", "floppy": "fd"}.get(element.get("device", "disk"))
    return None


def uses_per_device_boot(root: ET.Element) -> bool:
    devices = root.find("devices")
    return devices is not None and any(el.find("boot") is not None for el in devices)


def boot_order(root: ET.Element) -> List[str]:
    """Current order as device kinds, e.g. ['hd', 'cdrom']"""
    order: List[str] = []
    if uses_per_device_boot(root):
        entries = []
        for el in root.find("devices"):
            boot = el.find("boot")
            kind = _kind(el)
            if boot is not None and kind:
                entries.append((int(boot.get("order", "999")), kind))
        kinds = [kind for _n, kind in sorted(entries)]
    else:
        kinds = [b.get("dev") for b in root.findall("./os/boot") if b.get("dev")]
    for kind in kinds:
        if kind not in order:
            order.append(kind)
    return order or ["hd"]


def set_boot_order(root: ET.Element, order: List[str]) -> None:
    """Rewrite the boot order in place, keeping the XML's style (never mixing both)"""
    os_el = root.find("os")
    devices = root.find("devices")
    per_device = uses_per_device_boot(root)
    cdroms = [d for d in disk_elements(root) if d.get("device") == "cdrom"]
    if not per_device and "cdrom" in order and len(cdroms) > 1:
        per_device = True  # <boot dev='cdrom'/> would pick the first CD-ROM, maybe the seed

    targets = {
        "hd": boot_disk(root),
        "cdrom": user_cdrom(root) or (cdroms[0] if cdroms else None),
        "network": devices.find("interface") if devices is not None else None,
    }

    for boot in os_el.findall("boot"):
        os_el.remove(boot)
    if devices is not None:
        for el in devices:
            for boot in el.findall("boot"):
                el.remove(boot)

    if per_device:
        n = 1
        for kind in order:
            el = targets.get(kind)
            if el is not None:
                ET.SubElement(el, "boot", {"order": str(n)})
                n += 1
    else:
        # <boot> goes right after <type> (libvirt doesn't care, but keep it readable)
        children = list(os_el)
        index = next((i + 1 for i, c in enumerate(children) if c.tag == "type"), 0)
        for offset, kind in enumerate(order):
            boot = ET.Element("boot", {"dev": kind})
            boot.tail = (os_el.text or "\n")
            os_el.insert(index + offset, boot)
