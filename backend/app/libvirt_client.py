"""libvirt connection wrapper"""
import base64
import json
import time
import libvirt
import libvirt_qemu
import threading
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
from typing import Optional, List, Dict, Any, BinaryIO
import logging

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

        def on_network(_conn, net, event, _detail, _opaque):
            event_bus.publish({"kind": "network", "event": event, "name": net.name()})

        def on_pool(_conn, pool, event, _detail, _opaque):
            event_bus.publish({"kind": "pool", "event": event, "name": pool.name()})

        registrations = [
            lambda: conn.registerCloseCallback(on_close, None),
            lambda: conn.domainEventRegisterAny(None, libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE, on_domain, None),
            lambda: conn.domainEventRegisterAny(None, libvirt.VIR_DOMAIN_EVENT_ID_REBOOT, on_reboot, None),
            lambda: conn.networkEventRegisterAny(None, libvirt.VIR_NETWORK_EVENT_ID_LIFECYCLE, on_network, None),
            lambda: conn.storagePoolEventRegisterAny(None, libvirt.VIR_STORAGE_POOL_EVENT_ID_LIFECYCLE, on_pool, None),
        ]
        for register in registrations:
            try:
                register()
            except libvirt.libvirtError as e:
                logger.warning(f"Could not register libvirt event callback: {e}")

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
            root = ET.fromstring(domain.XMLDesc(0))
            for disk in root.findall("./devices/disk"):
                source = disk.find("source")
                path = source.get("file") if source is not None else None
                # Data disks and our generated cloud-init seed, never shared install ISOs
                if path and (disk.get("device") == "disk" or path.endswith("-cidata.iso")):
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

    def set_vm_metadata(self, name: str, uri: str, key: str, xml: Optional[str]) -> None:
        """Store (or remove with xml=None) a custom <metadata> element in the persistent config"""
        domain = self.connect().lookupByName(name)
        domain.setMetadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, xml, key if xml else None, uri,
                           libvirt.VIR_DOMAIN_AFFECT_CONFIG)

    def list_vm_metadata(self, uri: str) -> Dict[str, str]:
        """{vm name: metadata element XML} for every domain carrying metadata in namespace uri"""
        result = {}
        for dom in self.connect().listAllDomains(0):
            try:
                result[dom.name()] = dom.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, uri,
                                                  libvirt.VIR_DOMAIN_AFFECT_CONFIG)
            except libvirt.libvirtError:
                pass  # no metadata in that namespace
        return result

    # QEMU guest agent (the domain template has the channel; the guest needs qemu-guest-agent)

    def guest_agent(self, name: str, command: str, arguments: Optional[Dict[str, Any]] = None,
                    timeout: int = 10) -> Any:
        domain = self.connect().lookupByName(name)
        payload: Dict[str, Any] = {"execute": command}
        if arguments is not None:
            payload["arguments"] = arguments
        reply = libvirt_qemu.qemuAgentCommand(domain, json.dumps(payload), timeout, 0)
        return json.loads(reply).get("return")

    def guest_ping(self, name: str) -> bool:
        try:
            self.guest_agent(name, "guest-ping", timeout=5)
            return True
        except libvirt.libvirtError:
            return False

    def guest_exec(self, name: str, argv: List[str], timeout: float = 60) -> Dict[str, Any]:
        """Run a command in the guest: {exitcode, stdout, stderr}. Raises libvirtError / TimeoutError."""
        started = self.guest_agent(name, "guest-exec", {"path": argv[0], "arg": argv[1:], "capture-output": True})
        pid = started["pid"]
        deadline = time.monotonic() + timeout
        while True:
            status = self.guest_agent(name, "guest-exec-status", {"pid": pid})
            if status.get("exited"):
                def decode(key: str) -> str:
                    return base64.b64decode(status.get(key) or "").decode(errors="replace")
                return {"exitcode": status.get("exitcode", -1), "stdout": decode("out-data"),
                        "stderr": decode("err-data")}
            if time.monotonic() > deadline:
                raise TimeoutError(f"'{' '.join(argv)}' did not finish within {timeout:.0f}s in {name}")
            time.sleep(0.5)

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

    def get_vm_disks(self, name: str) -> List[Dict[str, Any]]:
        xml = self.get_vm_xml(name)
        if xml is None:
            return []
        disks = []
        for disk in ET.fromstring(xml).findall("./devices/disk"):
            source = disk.find("source")
            target = disk.find("target")
            disks.append({
                "device": disk.get("device"),
                "path": source.get("file") if source is not None else None,
                "target": target.get("dev") if target is not None else None,
            })
        return disks

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

    def update_dns_host(self, name: str, command: str, ip: str, hostnames: List[str]) -> None:
        """Add / delete a <dns><host> record, live (no restart) and in the saved config"""
        net = self.connect().networkLookupByName(name)
        commands = {"add": libvirt.VIR_NETWORK_UPDATE_COMMAND_ADD_LAST,
                    "delete": libvirt.VIR_NETWORK_UPDATE_COMMAND_DELETE}
        names = "".join(f"<hostname>{escape(h)}</hostname>" for h in hostnames)
        xml = f"<host ip={quoteattr(ip)}>{names}</host>"
        flags = libvirt.VIR_NETWORK_UPDATE_AFFECT_CONFIG
        if net.isActive():
            flags |= libvirt.VIR_NETWORK_UPDATE_AFFECT_LIVE
        net.update(commands[command], libvirt.VIR_NETWORK_SECTION_DNS_HOST, -1, xml, flags)

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
