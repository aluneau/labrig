"""libvirt connection wrapper"""
import libvirt
import threading
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
from typing import Optional, List, Dict, Any, BinaryIO
import logging

from app import domain_xml
from app.config import settings
from app.events import event_bus

logger = logging.getLogger(__name__)

# Errors are raised as libvirtError; don't also print them to stderr
libvirt.registerErrorHandler(lambda _ctx, _err: None, None)

DOMAIN_STATES = {
    libvirt.VIR_DOMAIN_NOSTATE: "nostate",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "blocked",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown",
    libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "pmsuspended",
}

DOMAIN_EVENTS = {
    libvirt.VIR_DOMAIN_EVENT_DEFINED: "defined",
    libvirt.VIR_DOMAIN_EVENT_UNDEFINED: "undefined",
    libvirt.VIR_DOMAIN_EVENT_STARTED: "started",
    libvirt.VIR_DOMAIN_EVENT_SUSPENDED: "suspended",
    libvirt.VIR_DOMAIN_EVENT_RESUMED: "resumed",
    libvirt.VIR_DOMAIN_EVENT_STOPPED: "stopped",
    libvirt.VIR_DOMAIN_EVENT_SHUTDOWN: "shutdown",
    libvirt.VIR_DOMAIN_EVENT_PMSUSPENDED: "pmsuspended",
    libvirt.VIR_DOMAIN_EVENT_CRASHED: "crashed",
}


def _start_event_loop() -> None:
    """libvirt delivers events (and keepalives) only while an event loop runs.
    It must be registered before the first connection is opened."""
    libvirt.virEventRegisterDefaultImpl()

    def run():
        while True:
            libvirt.virEventRunDefaultImpl()

    threading.Thread(target=run, daemon=True, name="libvirt-events").start()


_start_event_loop()

VOLUME_TYPES = {
    libvirt.VIR_STORAGE_VOL_FILE: "file",
    libvirt.VIR_STORAGE_VOL_BLOCK: "block",
    libvirt.VIR_STORAGE_VOL_DIR: "dir",
    libvirt.VIR_STORAGE_VOL_NETWORK: "network",
    libvirt.VIR_STORAGE_VOL_NETDIR: "netdir",
    libvirt.VIR_STORAGE_VOL_PLOOP: "ploop",
}


class LibvirtClient:
    """libvirt connection wrapper"""

    def __init__(self, uri: str = None):
        self.uri = uri or settings.LIBVIRT_URI
        self.conn: Optional[libvirt.virConnect] = None
        self._lock = threading.Lock()
        # (domain uuid, device alias) reported by DEVICE_REMOVED events, for detach_disk()
        self._removed = set()
        self._removed_cv = threading.Condition()

    def connect(self) -> libvirt.virConnect:
        """Connect to libvirt (reuses a live connection)"""
        with self._lock:
            if self.conn is None or not self.conn.isAlive():
                try:
                    self.conn = libvirt.open(self.uri)
                    logger.info(f"Connected to libvirt at {self.uri}")
                except libvirt.libvirtError as e:
                    logger.error(f"Failed to connect to libvirt: {e}")
                    raise
                self._register_events(self.conn)
                event_bus.publish({"kind": "connection", "event": "connected"})
            return self.conn

    def _register_events(self, conn: libvirt.virConnect) -> None:
        try:
            conn.setKeepAlive(5, 3)
        except libvirt.libvirtError:
            pass  # not supported by local-only drivers (e.g. test:///)

        def on_close(_conn, reason, _opaque):
            logger.warning(f"libvirt connection closed (reason {reason})")
            event_bus.publish({"kind": "connection", "event": "disconnected"})

        def on_domain(_conn, dom, event, _detail, _opaque):
            try:
                state = DOMAIN_STATES.get(dom.state()[0], "unknown")
            except libvirt.libvirtError:
                state = "undefined"
            event_bus.publish({"kind": "vm", "event": DOMAIN_EVENTS.get(event, str(event)),
                               "uuid": dom.UUIDString(), "name": dom.name(), "state": state})

        def on_reboot(_conn, dom, _opaque):
            event_bus.publish({"kind": "vm", "event": "rebooted", "uuid": dom.UUIDString(),
                               "name": dom.name(), "state": "running"})

        def on_device_removed(_conn, dom, alias, _opaque):
            with self._removed_cv:
                self._removed.add((dom.UUIDString(), alias))
                self._removed_cv.notify_all()
            self._publish_vm(dom, "device_removed", device=alias)

        def on_network(_conn, net, event, _detail, _opaque):
            event_bus.publish({"kind": "network", "event": event, "name": net.name()})

        def on_pool(_conn, pool, event, _detail, _opaque):
            event_bus.publish({"kind": "pool", "event": event, "name": pool.name()})

        registrations = [
            lambda: conn.registerCloseCallback(on_close, None),
            lambda: conn.domainEventRegisterAny(None, libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE, on_domain, None),
            lambda: conn.domainEventRegisterAny(None, libvirt.VIR_DOMAIN_EVENT_ID_REBOOT, on_reboot, None),
            lambda: conn.domainEventRegisterAny(None, libvirt.VIR_DOMAIN_EVENT_ID_DEVICE_REMOVED,
                                                on_device_removed, None),
            lambda: conn.networkEventRegisterAny(None, libvirt.VIR_NETWORK_EVENT_ID_LIFECYCLE, on_network, None),
            lambda: conn.storagePoolEventRegisterAny(None, libvirt.VIR_STORAGE_POOL_EVENT_ID_LIFECYCLE, on_pool, None),
        ]
        for register in registrations:
            try:
                register()
            except libvirt.libvirtError as e:
                logger.warning(f"Could not register libvirt event callback: {e}")

    @staticmethod
    def _publish_vm(dom: libvirt.virDomain, event: str, **extra) -> None:
        try:
            state = DOMAIN_STATES.get(dom.state()[0], "unknown")
        except libvirt.libvirtError:
            state = "undefined"
        event_bus.publish({"kind": "vm", "event": event, "uuid": dom.UUIDString(), "name": dom.name(),
                           "state": state, **extra})

    def disconnect(self):
        """Disconnect from libvirt"""
        with self._lock:
            if self.conn:
                self.conn.close()
                self.conn = None
                logger.info("Disconnected from libvirt")

    # VM Operations

    def _domain_dict(self, domain: libvirt.virDomain) -> Dict[str, Any]:
        state, max_mem, mem, vcpu, cpu_time = domain.info()
        return {
            "uuid": domain.UUIDString(),
            "name": domain.name(),
            "id": domain.ID(),
            "state": DOMAIN_STATES.get(state, "unknown"),
            "max_memory": max_mem,
            "memory": max_mem if state != libvirt.VIR_DOMAIN_RUNNING else mem,
            "vcpu": vcpu,
            "cpu_time": cpu_time,
        }

    def list_vms(self) -> List[Dict[str, Any]]:
        """List all VMs (running and defined)"""
        conn = self.connect()
        return [self._domain_dict(d) for d in conn.listAllDomains(0)]

    def get_vm(self, name: str) -> Optional[Dict[str, Any]]:
        """Get VM by name"""
        conn = self.connect()
        try:
            domain = conn.lookupByName(name)
        except libvirt.libvirtError:
            return None
        return {
            **self._domain_dict(domain),
            "xml": domain.XMLDesc(0),
            "autostart": bool(domain.autostart()),
        }

    def get_vm_xml(self, name: str) -> Optional[str]:
        try:
            return self.connect().lookupByName(name).XMLDesc(0)
        except libvirt.libvirtError:
            return None

    def create_vm(self, name: str, memory: int, vcpu: int,
                  disk_xml: str, network_xml: str,
                  arch: str = "x86_64", boot_devs: Optional[List[str]] = None) -> str:
        """Define a new VM. memory is in MiB."""
        conn = self.connect()
        boot_xml = "\n".join(f"<boot dev='{d}'/>" for d in (boot_devs or ["hd"]))
        listen = quoteattr(settings.VNC_LISTEN)
        # libvirt only creates as many pcie-root-ports as the devices need, and hot-plugging a
        # disk or NIC on q35 needs a free one: declare 16 (~8 spare after the built-in devices)
        root_ports = "<controller type='pci' index='0' model='pcie-root'/>" + \
            "<controller type='pci' model='pcie-root-port'/>" * 16

        # No <emulator> and machine='q35': libvirt resolves the emulator binary
        # and the latest q35 machine version from the host's capabilities.
        xml = f"""
        <domain type='kvm'>
            <name>{escape(name)}</name>
            <memory unit='MiB'>{int(memory)}</memory>
            <currentMemory unit='MiB'>{int(memory)}</currentMemory>
            <vcpu placement='static'>{int(vcpu)}</vcpu>
            <os>
                <type arch={quoteattr(arch)} machine='q35'>hvm</type>
                {boot_xml}
            </os>
            <features>
                <acpi/>
                <apic/>
            </features>
            <cpu mode='host-passthrough' check='none'/>
            <clock offset='utc'/>
            <on_poweroff>destroy</on_poweroff>
            <on_reboot>restart</on_reboot>
            <on_crash>destroy</on_crash>
            <devices>
                {disk_xml}
                {network_xml}
                {root_ports}
                <serial type='pty'>
                    <target port='0'/>
                </serial>
                <console type='pty'>
                    <target type='serial' port='0'/>
                </console>
                <channel type='unix'>
                    <target type='virtio' name='org.qemu.guest_agent.0'/>
                </channel>
                <input type='tablet' bus='usb'/>
                <graphics type='vnc' port='-1' autoport='yes' listen={listen}>
                    <listen type='address' address={listen}/>
                </graphics>
                <video>
                    <model type='virtio'/>
                </video>
                <rng model='virtio'>
                    <backend model='random'>/dev/urandom</backend>
                </rng>
                <vsock model='virtio'>
                    <cid auto='yes'/>
                </vsock>
            </devices>
        </domain>
        """

        domain = conn.defineXML(xml)
        logger.info(f"Defined VM {name} with UUID {domain.UUIDString()}")
        return domain.UUIDString()

    def start_vm(self, name: str) -> None:
        self.connect().lookupByName(name).create()
        logger.info(f"Started VM {name}")

    def stop_vm(self, name: str, force: bool = False) -> None:
        domain = self.connect().lookupByName(name)
        if force:
            domain.destroy()
        else:
            domain.shutdown()
        logger.info(f"Stopped VM {name} (force={force})")

    def reboot_vm(self, name: str) -> None:
        self.connect().lookupByName(name).reboot()
        logger.info(f"Rebooted VM {name}")

    def suspend_vm(self, name: str) -> None:
        self.connect().lookupByName(name).suspend()

    def resume_vm(self, name: str) -> None:
        self.connect().lookupByName(name).resume()

    def set_autostart(self, name: str, autostart: bool) -> None:
        self.connect().lookupByName(name).setAutostart(1 if autostart else 0)

    def delete_vm(self, name: str, delete_disks: bool = False) -> bool:
        """Delete a VM. Returns False if it did not exist in libvirt."""
        conn = self.connect()
        try:
            domain = conn.lookupByName(name)
        except libvirt.libvirtError:
            return False

        disk_paths = []
        if delete_disks:
            # Running + saved config: disks attached or detached for the next start count too
            roots = [ET.fromstring(domain.XMLDesc(0)),
                     ET.fromstring(domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))]
            for root in roots:
                for disk in domain_xml.disk_elements(root):
                    path = domain_xml.disk_info(disk)["path"]
                    # Data disks and our generated cloud-init seed, never shared install ISOs
                    if path and path not in disk_paths and (disk.get("device") == "disk" or domain_xml.is_seed(path)):
                        disk_paths.append(path)

        if domain.isActive():
            domain.destroy()
        try:
            domain.undefineFlags(
                libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
                | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
                | libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
            )
        except libvirt.libvirtError:
            domain.undefine()  # driver without support for those flags

        for path in disk_paths:
            try:
                conn.storageVolLookupByPath(path).delete(0)
            except libvirt.libvirtError as e:
                logger.warning(f"Could not delete disk {path}: {e}")

        logger.info(f"Deleted VM {name}")
        return True

    def get_vm_console(self, name: str) -> Optional[Dict[str, Any]]:
        """Get VM graphical console information"""
        xml = self.get_vm_xml(name)
        if xml is None:
            return None
        graphics = ET.fromstring(xml).find("./devices/graphics")
        if graphics is None:
            return None
        port = int(graphics.get("port", "-1"))
        listen = graphics.get("listen", "127.0.0.1")
        return {
            "type": graphics.get("type", "vnc"),
            "host": listen if listen not in ("0.0.0.0", "::") else "127.0.0.1",
            "port": port if port > 0 else None,
        }

    def get_vm_nics(self, name: str) -> List[Dict[str, Any]]:
        """Configured NICs (available even when the VM is off)"""
        xml = self.get_vm_xml(name)
        if xml is None:
            return []
        nics = []
        for iface in ET.fromstring(xml).findall("./devices/interface"):
            source, mac = iface.find("source"), iface.find("mac")
            nics.append({
                "network": source.get("network") or source.get("bridge") if source is not None else None,
                "mac": mac.get("address") if mac is not None else None,
            })
        return nics

    def get_vm_interfaces(self, name: str) -> List[Dict[str, Any]]:
        """Interfaces with IPs from DHCP leases (only for running VMs)"""
        conn = self.connect()
        try:
            domain = conn.lookupByName(name)
        except libvirt.libvirtError:
            return []
        if not domain.isActive():
            return []
        try:
            ifaces = domain.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE)
        except libvirt.libvirtError:
            return []
        return [
            {
                "name": iface_name,
                "mac": data.get("hwaddr"),
                "addresses": [a["addr"] for a in (data.get("addrs") or [])],
            }
            for iface_name, data in ifaces.items()
        ]

    # VM devices: CD-ROM, boot order, disks

    def _domain(self, name: str) -> libvirt.virDomain:
        return self.connect().lookupByName(name)

    @staticmethod
    def _xml_roots(domain: libvirt.virDomain):
        """(running XML or None if shut off, saved persistent XML)"""
        live = ET.fromstring(domain.XMLDesc(0)) if domain.isActive() else None
        config = ET.fromstring(domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE | libvirt.VIR_DOMAIN_XML_SECURE))
        return live, config

    def get_vm_devices(self, name: str) -> Optional[Dict[str, Any]]:
        """Disks (with size and pending changes), the user CD-ROM and the boot order"""
        try:
            domain = self._domain(name)
        except libvirt.libvirtError:
            return None
        live, config = self._xml_roots(domain)
        current = live if live is not None else config
        boot = domain_xml.boot_disk(config)
        boot_target = domain_xml.disk_info(boot)["target"] if boot is not None else None

        disks = []
        seen = set()
        for root in (current, config):
            for el in domain_xml.disk_elements(root):
                info = domain_xml.disk_info(el)
                if info["target"] in seen:
                    continue
                seen.add(info["target"])
                info.pop("alias")
                info.pop("boot_order")
                info["boot"] = info["target"] == boot_target
                info["pending"] = None
                if live is not None:
                    in_live = domain_xml.find_disk(live, info["target"]) is not None
                    in_config = domain_xml.find_disk(config, info["target"]) is not None
                    info["pending"] = "attach" if not in_live else "detach" if not in_config else None
                info["capacity"] = self._disk_capacity(
                    domain, info, in_live=live is not None and info["pending"] != "attach")
                disks.append(info)

        cdrom = None
        cd_config = domain_xml.user_cdrom(config)
        cd_live = domain_xml.user_cdrom(live) if live is not None else None
        if cd_config is not None or cd_live is not None:
            info = domain_xml.disk_info(cd_live if cd_live is not None else cd_config)
            cdrom = {"target": info["target"], "path": info["path"],
                     # added while running: the device only exists from the next start
                     "pending": live is not None and cd_live is None}
        return {"disks": disks, "cdrom": cdrom, "boot_order": domain_xml.boot_order(config)}

    def _disk_capacity(self, domain: libvirt.virDomain, info: Dict[str, Any], in_live: bool) -> Optional[int]:
        if info["device"] != "disk" or not info["path"]:
            return None
        try:
            if in_live:
                return domain.blockInfo(info["target"])[0]
            return self.connect().storageVolLookupByPath(info["path"]).info()[1]
        except libvirt.libvirtError:
            return None

    def set_cdrom(self, name: str, iso_path: Optional[str]) -> Dict[str, Any]:
        """Insert (or eject with None) the user CD-ROM's media; adds a SATA CD-ROM if the VM has none.

        Returns {"target", "pending"}: pending=True when it only applies at the next start.
        """
        domain = self._domain(name)
        live, config = self._xml_roots(domain)
        cd_config = domain_xml.user_cdrom(config)
        cd_live = domain_xml.user_cdrom(live) if live is not None else None

        if cd_config is None and cd_live is None:
            if not iso_path:
                return {"target": None, "pending": False}
            # SATA CD-ROMs can't be hot-plugged: add it to the saved config
            target = domain_xml.next_target([r for r in (live, config) if r is not None], "sd")
            domain.attachDeviceFlags(domain_xml.cdrom_xml(target, iso_path), libvirt.VIR_DOMAIN_AFFECT_CONFIG)
            self._publish_vm(domain, "devices")
            return {"target": target, "pending": live is not None, "added": True}

        if cd_live is not None:
            # Two calls: the running and saved definitions may differ (e.g. a one-shot boot order
            # puts <boot order='1'/> on the running CD-ROM only). FORCE: eject even if the tray is locked.
            domain.updateDeviceFlags(domain_xml.media_change_xml(cd_live, iso_path),
                                     libvirt.VIR_DOMAIN_AFFECT_LIVE | libvirt.VIR_DOMAIN_DEVICE_MODIFY_FORCE)
            if cd_config is not None:
                domain.updateDeviceFlags(domain_xml.media_change_xml(cd_config, iso_path),
                                         libvirt.VIR_DOMAIN_AFFECT_CONFIG)
            pending = False
        else:
            domain.updateDeviceFlags(domain_xml.media_change_xml(cd_config, iso_path),
                                     libvirt.VIR_DOMAIN_AFFECT_CONFIG)
            pending = live is not None
        self._publish_vm(domain, "devices")
        target = domain_xml.disk_info(cd_live if cd_live is not None else cd_config)["target"]
        return {"target": target, "pending": pending, "added": False}

    def set_boot_order(self, name: str, order: List[str]) -> None:
        """Persistent boot order (applies at the next cold start)"""
        domain = self._domain(name)
        _live, config = self._xml_roots(domain)
        domain_xml.set_boot_order(config, order)
        self.connect().defineXML(ET.tostring(config, encoding="unicode"))
        self._publish_vm(domain, "devices")

    def start_vm_with_boot_order(self, name: str, order: List[str]) -> None:
        """One-shot boot order: define it, start, then restore the saved definition.

        The running instance keeps this order (guest reboots too); the next cold start uses the original.
        """
        conn = self.connect()
        domain = conn.lookupByName(name)
        original = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE | libvirt.VIR_DOMAIN_XML_SECURE)
        once = ET.fromstring(original)
        domain_xml.set_boot_order(once, order)
        conn.defineXML(ET.tostring(once, encoding="unicode"))
        try:
            domain.create()
        finally:
            conn.defineXML(original)
        logger.info(f"Started VM {name} with one-shot boot order {order}")

    def attach_disk(self, name: str, xml: str) -> Dict[str, Any]:
        """Attach a disk to the saved config and, if running, live (hot-plug).

        Returns {"pending", "error"}: pending=True when it only applies at the next start
        (error = why hot-plug failed, if it was tried).
        """
        domain = self._domain(name)
        if domain.isActive():
            try:
                domain.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_LIVE | libvirt.VIR_DOMAIN_AFFECT_CONFIG)
                self._publish_vm(domain, "devices")
                return {"pending": False, "error": None}
            except libvirt.libvirtError as e:
                logger.warning(f"Hot-plug into {name} failed, attaching for next start: {e}")
                domain.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG)
                self._publish_vm(domain, "devices")
                return {"pending": True, "error": e.get_error_message() or str(e)}
        domain.attachDeviceFlags(xml, libvirt.VIR_DOMAIN_AFFECT_CONFIG)
        self._publish_vm(domain, "devices")
        return {"pending": False, "error": None}

    def detach_disk(self, name: str, target: str, timeout: float = 15.0) -> Dict[str, Any]:
        """Detach a disk; when running, hot-unplug and wait for the guest to release it.

        Returns {"pending", "path"}: pending=True when the guest didn't release it in time (or the bus
        can't hot-unplug). It is removed from the saved config either way, so it goes away at the
        next shutdown.
        """
        domain = self._domain(name)
        live, config = self._xml_roots(domain)
        el_config = domain_xml.find_disk(config, target)
        el_live = domain_xml.find_disk(live, target) if live is not None else None
        if el_config is None and el_live is None:
            raise ValueError(f"No disk {target}")
        path = domain_xml.disk_info(el_live if el_live is not None else el_config)["path"]

        if el_live is None:
            domain.detachDeviceFlags(ET.tostring(el_config, encoding="unicode"), libvirt.VIR_DOMAIN_AFFECT_CONFIG)
            self._publish_vm(domain, "devices")
            return {"pending": False, "path": path}

        key = (domain.UUIDString(), domain_xml.disk_info(el_live)["alias"])
        with self._removed_cv:
            self._removed.discard(key)
        flags = libvirt.VIR_DOMAIN_AFFECT_LIVE | (libvirt.VIR_DOMAIN_AFFECT_CONFIG if el_config is not None else 0)
        try:
            domain.detachDeviceFlags(ET.tostring(el_live, encoding="unicode"), flags)
        except libvirt.libvirtError as e:
            if el_config is None:
                raise
            logger.warning(f"Hot-unplug of {target} from {name} refused, detaching at next shutdown: {e}")
            domain.detachDeviceFlags(ET.tostring(el_config, encoding="unicode"), libvirt.VIR_DOMAIN_AFFECT_CONFIG)
            self._publish_vm(domain, "devices")
            return {"pending": True, "path": path}

        def gone() -> bool:
            return not domain.isActive() or domain_xml.find_disk(ET.fromstring(domain.XMLDesc(0)), target) is None

        # The guest must acknowledge the unplug: wait for libvirt's DEVICE_REMOVED event
        deadline = time.monotonic() + timeout
        with self._removed_cv:
            while key not in self._removed and time.monotonic() < deadline:
                self._removed_cv.wait(min(deadline - time.monotonic(), 2.0))
                if key not in self._removed and gone():
                    break  # event missed (e.g. libvirt reconnected); the device is gone anyway
            self._removed.discard(key)
        removed = gone()
        self._publish_vm(domain, "devices")
        return {"pending": not removed, "path": path}

    def resize_disk(self, name: str, target: str, capacity: int) -> None:
        """Grow a disk to capacity bytes: blockResize when running (QEMU owns the image), else vol.resize"""
        domain = self._domain(name)
        live, config = self._xml_roots(domain)
        el_live = domain_xml.find_disk(live, target) if live is not None else None
        el = el_live if el_live is not None else domain_xml.find_disk(config, target)
        if el is None or el.get("device") != "disk":
            raise ValueError(f"No disk {target}")
        if el_live is not None:
            domain.blockResize(target, int(capacity), libvirt.VIR_DOMAIN_BLOCK_RESIZE_BYTES)
        else:
            self.connect().storageVolLookupByPath(domain_xml.disk_info(el)["path"]).resize(int(capacity), 0)
        self._publish_vm(domain, "devices")

    def next_disk_target(self, name: str, prefix: str) -> str:
        """First free vdX / sdX across the running and saved configs"""
        live, config = self._xml_roots(self._domain(name))
        return domain_xml.next_target([r for r in (live, config) if r is not None], prefix)

    def publish_vm_event(self, name: str, event: str) -> None:
        try:
            self._publish_vm(self._domain(name), event)
        except libvirt.libvirtError:
            pass

    def free_volume_name(self, pool_name: str, stem: str, ext: str) -> str:
        """<stem>1.<ext>, <stem>2.<ext>, ...: first name not taken in the pool"""
        pool = self.connect().storagePoolLookupByName(pool_name)
        try:
            pool.refresh(0)  # files created outside libvirt
        except libvirt.libvirtError:
            pass  # refused while another volume job (e.g. a clone) runs in the pool
        taken = set(pool.listVolumes())
        n = 1
        while f"{stem}{n}.{ext}" in taken:
            n += 1
        return f"{stem}{n}.{ext}"

    # Storage Operations

    def _pool_dict(self, pool: libvirt.virStoragePool) -> Dict[str, Any]:
        _state, capacity, allocation, available = pool.info()
        xml = pool.XMLDesc(0)
        root = ET.fromstring(xml)
        path = root.findtext("./target/path")
        return {
            "uuid": pool.UUIDString(),
            "name": pool.name(),
            "type": root.get("type", "dir"),
            "path": path,
            "state": "active" if pool.isActive() else "inactive",
            "capacity": capacity,
            "allocation": allocation,
            "available": available,
            "autostart": bool(pool.autostart()),
            "xml": xml,
        }

    def list_storage_pools(self) -> List[Dict[str, Any]]:
        """List all storage pools (active and inactive)"""
        conn = self.connect()
        pools = []
        for pool in conn.listAllStoragePools(0):
            if pool.isActive():
                try:
                    pool.refresh(0)
                except libvirt.libvirtError:
                    pass
            pools.append(self._pool_dict(pool))
        return pools

    def create_storage_pool(self, name: str, path: str,
                            pool_type: str = "dir", autostart: bool = True) -> str:
        """Define, build and start a directory storage pool"""
        conn = self.connect()
        xml = f"""
        <pool type={quoteattr(pool_type)}>
            <name>{escape(name)}</name>
            <target>
                <path>{escape(path)}</path>
            </target>
        </pool>
        """
        pool = conn.storagePoolDefineXML(xml, 0)
        try:
            pool.build(libvirt.VIR_STORAGE_POOL_BUILD_NO_OVERWRITE)
        except libvirt.libvirtError:
            pass  # directory already exists
        pool.create()
        pool.setAutostart(1 if autostart else 0)
        logger.info(f"Created storage pool {name}")
        return pool.UUIDString()

    def ensure_pool(self, name: str, path: str) -> libvirt.virStoragePool:
        """Return an active pool by name, creating it if needed"""
        conn = self.connect()
        try:
            pool = conn.storagePoolLookupByName(name)
        except libvirt.libvirtError:
            same_path = [p for p in conn.listAllStoragePools(0)
                         if ET.fromstring(p.XMLDesc(0)).findtext("./target/path") == path]
            if same_path:
                pool = same_path[0]
            else:
                self.create_storage_pool(name, path)
                pool = conn.storagePoolLookupByName(name)
        if not pool.isActive():
            pool.create()
        return pool

    def start_storage_pool(self, name: str) -> None:
        self.connect().storagePoolLookupByName(name).create()

    def stop_storage_pool(self, name: str) -> None:
        self.connect().storagePoolLookupByName(name).destroy()

    def delete_storage_pool(self, name: str) -> bool:
        """Undefine a storage pool (keeps its files on disk)"""
        conn = self.connect()
        try:
            pool = conn.storagePoolLookupByName(name)
        except libvirt.libvirtError:
            return False
        if pool.isActive():
            pool.destroy()
        pool.undefine()
        logger.info(f"Deleted storage pool {name}")
        return True

    def list_volumes(self, pool_name: str) -> List[Dict[str, Any]]:
        """List volumes in a pool"""
        pool = self.connect().storagePoolLookupByName(pool_name)
        if not pool.isActive():
            return []
        volumes = []
        for vol in pool.listAllVolumes(0):
            vol_type, capacity, allocation = vol.info()
            root = ET.fromstring(vol.XMLDesc(0))
            fmt = root.find("./target/format")
            volumes.append({
                "name": vol.name(),
                "key": vol.key(),
                "path": vol.path(),
                "type": VOLUME_TYPES.get(vol_type, "file"),
                "format": fmt.get("type") if fmt is not None else None,
                "capacity": capacity,
                "allocation": allocation,
            })
        return volumes

    def create_volume(self, pool_name: str, name: str,
                      capacity: int, format: str = "qcow2") -> str:
        """Create a volume, returns its path. capacity is in bytes."""
        pool = self.connect().storagePoolLookupByName(pool_name)
        xml = f"""
        <volume>
            <name>{escape(name)}</name>
            <capacity unit='bytes'>{int(capacity)}</capacity>
            <target>
                <format type={quoteattr(format)}/>
            </target>
        </volume>
        """
        vol = pool.createXML(xml, 0)
        logger.info(f"Created volume {name} in pool {pool_name}")
        return vol.path()

    def upload_volume(self, pool_name: str, name: str, size: int,
                      fileobj: BinaryIO, format: str = "raw") -> str:
        """Create a volume and stream file content into it, returns its path"""
        conn = self.connect()
        path = self.create_volume(pool_name, name, size, format)
        vol = conn.storageVolLookupByPath(path)
        stream = conn.newStream(0)
        try:
            vol.upload(stream, 0, size, 0)
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                stream.send(chunk)
            stream.finish()
        except Exception:
            try:
                stream.abort()
            except libvirt.libvirtError:
                pass
            vol.delete(0)
            raise
        logger.info(f"Uploaded volume {name} to pool {pool_name}")
        return path

    def refresh_pool(self, pool_name: str) -> None:
        """Rescan a pool (picks up the real format of uploaded images)"""
        self.connect().storagePoolLookupByName(pool_name).refresh(0)

    def clone_volume(self, pool_name: str, source_path: str, name: str, capacity: int) -> str:
        """Full qcow2 copy of source_path, grown to at least capacity bytes; returns its path"""
        conn = self.connect()
        source = conn.storageVolLookupByPath(source_path)
        pool = conn.storagePoolLookupByName(pool_name)
        xml = f"""
        <volume>
            <name>{escape(name)}</name>
            <capacity unit='bytes'>{int(source.info()[1])}</capacity>
            <target>
                <format type='qcow2'/>
            </target>
        </volume>
        """
        vol = pool.createXMLFrom(xml, source, 0)
        if capacity > vol.info()[1]:
            try:
                vol.resize(int(capacity), 0)
            except libvirt.libvirtError:
                vol.delete(0)
                raise
        logger.info(f"Cloned {source_path} to {name} in pool {pool_name}")
        return vol.path()

    def volume_exists(self, path: Optional[str]) -> bool:
        if not path:
            return False
        try:
            self.connect().storageVolLookupByPath(path)
            return True
        except libvirt.libvirtError:
            return False

    def volume_capacity(self, path: str) -> int:
        return self.connect().storageVolLookupByPath(path).info()[1]

    def delete_volume_by_path(self, path: str) -> bool:
        try:
            self.connect().storageVolLookupByPath(path).delete(0)
            return True
        except libvirt.libvirtError:
            return False

    def delete_volume(self, pool_name: str, name: str) -> bool:
        conn = self.connect()
        try:
            vol = conn.storagePoolLookupByName(pool_name).storageVolLookupByName(name)
        except libvirt.libvirtError:
            return False
        vol.delete(0)
        logger.info(f"Deleted volume {name} from pool {pool_name}")
        return True

    # Network Operations

    def list_networks(self) -> List[Dict[str, Any]]:
        """List all networks (active and inactive)"""
        conn = self.connect()
        return [
            {
                "uuid": net.UUIDString(),
                "name": net.name(),
                "active": bool(net.isActive()),
                "autostart": bool(net.autostart()),
                "persistent": bool(net.isPersistent()),
                "xml": net.XMLDesc(0),
            }
            for net in conn.listAllNetworks(0)
        ]

    def create_network(self, name: str, xml: str, autostart: bool = True) -> str:
        net = self.connect().networkDefineXML(xml)
        net.create()
        net.setAutostart(1 if autostart else 0)
        logger.info(f"Created network {name}")
        return net.UUIDString()

    def start_network(self, name: str) -> None:
        self.connect().networkLookupByName(name).create()

    def stop_network(self, name: str) -> None:
        self.connect().networkLookupByName(name).destroy()

    def set_network_autostart(self, name: str, autostart: bool) -> None:
        self.connect().networkLookupByName(name).setAutostart(1 if autostart else 0)

    def get_network_leases(self, name: str) -> List[Dict[str, Any]]:
        net = self.connect().networkLookupByName(name)
        if not net.isActive():
            return []
        return net.DHCPLeases()

    def get_network_xml(self, name: str) -> str:
        return self.connect().networkLookupByName(name).XMLDesc(0)

    def redefine_network(self, xml: str, restart: bool) -> None:
        """Replace a network's persistent definition; restart it to apply if it is running"""
        conn = self.connect()
        net = conn.networkDefineXML(xml)
        if restart and net.isActive():
            net.destroy()
            net.create()

    def restart_network(self, name: str) -> None:
        net = self.connect().networkLookupByName(name)
        if net.isActive():
            net.destroy()
        net.create()

    def update_dhcp_host(self, name: str, command: str, mac: str, ip: str, hostname: Optional[str]) -> None:
        """Add / modify / delete a static DHCP host, live (no restart) and in the saved config"""
        net = self.connect().networkLookupByName(name)
        commands = {
            "add": libvirt.VIR_NETWORK_UPDATE_COMMAND_ADD_LAST,
            "modify": libvirt.VIR_NETWORK_UPDATE_COMMAND_MODIFY,
            "delete": libvirt.VIR_NETWORK_UPDATE_COMMAND_DELETE,
        }
        name_attr = f" name={quoteattr(hostname)}" if hostname else ""
        xml = f"<host mac={quoteattr(mac)}{name_attr} ip={quoteattr(ip)}/>"
        flags = libvirt.VIR_NETWORK_UPDATE_AFFECT_CONFIG
        if net.isActive():
            flags |= libvirt.VIR_NETWORK_UPDATE_AFFECT_LIVE
        net.update(commands[command], libvirt.VIR_NETWORK_SECTION_IP_DHCP_HOST, -1, xml, flags)

    def network_interfaces(self, name: str) -> List[Dict[str, Any]]:
        """VM interfaces attached to a network: [{vm, mac}]"""
        result = []
        for dom in self.connect().listAllDomains(0):
            root = ET.fromstring(dom.XMLDesc(0))
            for iface in root.findall("./devices/interface[@type='network']"):
                source, mac = iface.find("source"), iface.find("mac")
                if source is not None and source.get("network") == name and mac is not None:
                    result.append({"vm": dom.name(), "mac": mac.get("address")})
        return result

    def delete_network(self, name: str) -> bool:
        conn = self.connect()
        try:
            net = conn.networkLookupByName(name)
        except libvirt.libvirtError:
            return False
        if net.isActive():
            net.destroy()
        net.undefine()
        logger.info(f"Deleted network {name}")
        return True

    # Host Info

    def get_host_info(self) -> Dict[str, Any]:
        conn = self.connect()
        model, memory, cpus, mhz, nodes, sockets, cores, threads = conn.getInfo()
        caps = ET.fromstring(conn.getCapabilities())
        return {
            "hostname": conn.getHostname(),
            "arch": caps.findtext("./host/cpu/arch") or model,
            "cpu_model": caps.findtext("./host/cpu/model"),
            "memory": memory,  # MiB
            "cpus": cpus,
            "mhz": mhz,
            "nodes": nodes,
            "sockets": sockets,
            "cores": cores,
            "threads": threads,
            "libvirt_version": conn.getLibVersion(),
            "hypervisor_version": conn.getVersion(),
            "emulator_available": caps.find("./guest") is not None,
        }

    def get_capabilities_xml(self) -> str:
        return self.connect().getCapabilities()


libvirt_client = LibvirtClient()
