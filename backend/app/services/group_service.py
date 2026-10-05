"""Lab groups: an isolated network + a router VM + member VMs + a DNS zone

    network  vmm-g-<name>     isolated libvirt network, no libvirt DHCP/DNS
    router   <name>-rtr       eth0 -> uplink (NAT), eth1 -> group network (.1), dnsmasq + nftables
    members  <name>-<member>  fixed MACs, static leases + DNS names from the router

The spec (GroupSpec) is the source of truth for a group. It is stored in the
DB and in the router domain's <metadata> (members and the network carry their
group name and role), so sync_groups() can rebuild the DB from libvirt.
Spec changes are rendered by the router backend and pushed live through the
QEMU guest agent.
"""
import ipaddress
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

import libvirt
import yaml
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import serialized
from app.events import event_bus
from app.libvirt_client import libvirt_client
from app.models import CloudImage, Group, GroupMember, Network, Task, VM
from app.schemas import TaskCreate, VMCreate
from app.schemas.group import DHCPHostSpec, DHCPRange, DNSRecord, GroupSpec, MemberSpec
from app.services.cloud_image_service import cloud_image_service
from app.services.router_service import EL_IMAGES, STATE_DIR, get_backend
from app.services.task_service import task_service
from app.services.vm_service import vm_service

logger = logging.getLogger(__name__)

ROUTER_READY_TIMEOUT = 15 * 60  # first boot installs packages
ROUTER_RESTART_TIMEOUT = 5 * 60
SHUTDOWN_TIMEOUT = 120
LEASES_FILE = "/var/lib/dnsmasq/dnsmasq.leases"

# Member fields that can't change in place (the VM must be recreated)
MEMBER_IMMUTABLE = ("image", "memory", "vcpu", "disk_size", "role", "mac", "cloud_init", "user_data")


class LeaseInUse(Exception):
    """The lease belongs to a running VM"""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def network_name(group: str) -> str:
    return f"vmm-g-{group}"


def router_vm_name(group: str) -> str:
    return f"{group}-rtr"


def member_vm_name(group: str, member: str) -> str:
    return f"{group}-{member}"


class GroupService:

    def __init__(self):
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def _lock(self, name: str) -> threading.Lock:
        """One spec change / push at a time per group"""
        with self._locks_lock:
            return self._locks.setdefault(name, threading.Lock())

    # Lookup

    def get_group(self, db: Session, group_id: int) -> Optional[Group]:
        return db.query(Group).filter(Group.id == group_id).first()

    def list_groups(self, db: Session) -> List[Group]:
        self.sync_groups(db)
        return db.query(Group).order_by(Group.name).all()

    def _publish(self, group: Group, event: str) -> None:
        event_bus.publish({"kind": "group", "event": event, "id": group.id, "name": group.name,
                           "status": group.status})

    # Images

    def resolve_image(self, db: Session, ref: str) -> CloudImage:
        """'debian-13', 'cloudimg-debian-13.qcow2' or an id -> a ready cloud image"""
        images = [i for i in cloud_image_service.list_images(db) if i.status == "ready"]
        for image in images:
            slug = f"{image.distribution}-{image.version}".lower()
            if ref.lower() in (slug, image.name.lower(), f"cloudimg-{slug}.qcow2", str(image.id)):
                return image
        known = ", ".join(sorted(f"{i.distribution}-{i.version}" for i in images)) or "none"
        raise ValueError(f"Cloud image '{ref}' is not downloaded (ready images: {known}). "
                         "Download it on the Storage page.")

    def _default_router_image(self, db: Session) -> str:
        ready = {f"{i.distribution}-{i.version}".lower() for i in cloud_image_service.list_images(db)
                 if i.status == "ready"}
        for slug in EL_IMAGES:
            if slug in ready:
                return slug
        raise ValueError("The router needs an EL cloud image (AlmaLinux 9/10, Rocky 9 or CentOS Stream): "
                         "download one on the Storage page")

    # Spec normalization

    @staticmethod
    def _default_dhcp(net: ipaddress.IPv4Network) -> Tuple[str, str]:
        hosts = list(net.hosts())
        if len(hosts) >= 200:
            return str(hosts[99]), str(hosts[min(198, len(hosts) - 2)])
        return str(hosts[len(hosts) // 2]), str(hosts[-2])

    @staticmethod
    def _new_mac(taken: set) -> str:
        while True:
            mac = "52:54:00:%02x:%02x:%02x" % (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            if mac not in taken:
                taken.add(mac)
                return mac

    def normalize(self, db: Session, spec: GroupSpec, existing: Optional[GroupSpec] = None,
                  group_id: Optional[int] = None) -> GroupSpec:
        """Validate against the host and fill in what the app assigns: router image / IP / MACs,
        DHCP range, member IPs and MACs (kept from `existing` for unchanged names)."""
        spec = spec.model_copy(deep=True)
        backend = get_backend(spec.router.flavour)
        net = ipaddress.IPv4Network(spec.cidr)
        hosts = list(net.hosts())

        if existing is None:
            self._check_cidr(db, net, group_id)
            if spec.uplink and spec.uplink not in {n["name"] for n in libvirt_client.list_networks()}:
                raise ValueError(f"Uplink network '{spec.uplink}' does not exist")

        router = spec.router
        if existing is not None:
            old = existing.router
            router.ip = router.ip or old.ip
            router.lan_mac = router.lan_mac or old.lan_mac
            router.uplink_mac = router.uplink_mac or old.uplink_mac
            router.image = router.image or old.image
        router.ip = router.ip or str(hosts[0])
        router.image = router.image or self._default_router_image(db)
        self.resolve_image(db, router.image)

        if spec.dhcp is None:
            if existing is not None and existing.dhcp is not None:
                spec.dhcp = existing.dhcp
            else:
                start, end = self._default_dhcp(net)
                spec.dhcp = DHCPRange(start=start, end=end)

        taken_macs = set(libvirt_client.all_macs())
        old_members = {m.name: m for m in (existing.members if existing else [])}
        own_macs = {m.mac for m in old_members.values() if m.mac} | {router.lan_mac, router.uplink_mac}
        taken_macs -= own_macs  # our own VMs' MACs are fine to keep
        router.lan_mac = router.lan_mac or self._new_mac(taken_macs)
        router.uplink_mac = router.uplink_mac or self._new_mac(taken_macs)
        taken_macs |= {router.lan_mac, router.uplink_mac}

        dhcp_start, dhcp_end = ipaddress.IPv4Address(spec.dhcp.start), ipaddress.IPv4Address(spec.dhcp.end)
        used_ips = {router.ip} | {m.ip for m in spec.members if m.ip} | {h.ip for h in spec.dhcp_hosts}
        static_pool = [h for h in hosts[9 if len(hosts) >= 64 else 1:] if not dhcp_start <= h <= dhcp_end]
        for m in spec.members:
            prev = old_members.get(m.name)
            if not m.mac:
                m.mac = prev.mac if prev and prev.mac else None
            if m.mac and m.mac in taken_macs:
                raise ValueError(f"MAC {m.mac} of member {m.name} is already used on this host")
            m.mac = m.mac or self._new_mac(taken_macs)
            taken_macs.add(m.mac)
            if not m.ip and prev and prev.ip and prev.ip not in used_ips:
                m.ip = prev.ip
            if not m.ip:
                free = next((h for h in static_pool if str(h) not in used_ips), None)
                if free is None:
                    raise ValueError(f"No free address left in {net} outside the DHCP range for member {m.name}")
                m.ip = str(free)
            if dhcp_start <= ipaddress.IPv4Address(m.ip) <= dhcp_end:
                raise ValueError(f"Member {m.name} IP {m.ip} is inside the dynamic DHCP range "
                                 f"{spec.dhcp.start}-{spec.dhcp.end}")
            used_ips.add(m.ip)
            self.resolve_image(db, m.image)

        spec = GroupSpec.model_validate(spec.model_dump())  # re-run cross-field checks
        backend.validate(spec)
        return spec

    def _check_cidr(self, db: Session, net: ipaddress.IPv4Network, group_id: Optional[int]) -> None:
        for other in libvirt_client.network_subnets():
            if net.overlaps(ipaddress.IPv4Network(other["cidr"])):
                raise ValueError(f"{net} overlaps network '{other['network']}' ({other['cidr']})")
        for group in db.query(Group).all():
            if group.id != group_id and net.overlaps(ipaddress.IPv4Network(group.cidr)):
                raise ValueError(f"{net} overlaps group '{group.name}' ({group.cidr})")

    # Create

    def create_group(self, db: Session, spec: GroupSpec) -> Tuple[Group, Task]:
        self.sync_groups(db)
        if db.query(Group).filter(Group.name == spec.name).first():
            raise ValueError(f"Group '{spec.name}' already exists")
        net_names = {n["name"] for n in libvirt_client.list_networks()}
        if network_name(spec.name) in net_names:
            raise ValueError(f"Network '{network_name(spec.name)}' already exists")
        vm_names = [router_vm_name(spec.name)] + [member_vm_name(spec.name, m.name) for m in spec.members]
        for name in vm_names:
            if libvirt_client.get_vm(name) is not None:
                raise ValueError(f"A VM named '{name}' already exists")

        spec = self.normalize(db, spec)
        group = Group(name=spec.name, cidr=spec.cidr, domain=spec.domain, uplink=spec.uplink,
                      router_vm_name=router_vm_name(spec.name), spec=spec.model_dump(mode="json"),
                      status="creating")
        db.add(group)
        db.commit()
        self._sync_member_rows(db, group, spec)
        self._publish(group, "created")

        task = task_service.start(
            db,
            TaskCreate(name=f"Create lab group {spec.name}", type="group_create", target_type="group",
                       target_id=group.id, target_name=spec.name,
                       description=f"{spec.cidr}, router + {len(spec.members)} member(s)"),
            self._create_task, group.id,
        )
        return group, task

    def _create_task(self, db: Session, task: Task, group_id: int) -> Dict[str, Any]:
        group = self.get_group(db, group_id)
        spec = GroupSpec.model_validate(group.spec)
        progress = lambda pct: task_service.update_progress(db, task.id, pct)  # noqa: E731

        def check():
            db.expire_all()
            g = self.get_group(db, group_id)
            if g is None or g.status == "deleting":
                raise RuntimeError("The group was deleted")
            if task_service.is_cancelled(task.id):
                raise RuntimeError("Cancelled")

        try:
            self._ensure_network(spec)
            progress(5)
            check()
            self._create_router(db, spec, start=True)
            group.router_vm_id = self._vm_id(db, router_vm_name(spec.name))
            db.commit()
            progress(10)

            started = time.monotonic()

            def waiting():
                check()
                elapsed = time.monotonic() - started
                progress(10 + int(50 * min(elapsed / 300, 1)))  # first boot takes ~2-5 min

            self._wait_router(router_vm_name(spec.name), ROUTER_READY_TIMEOUT, waiting)
            group.config_applied, group.config_applied_at, group.config_error = True, _now(), None
            db.commit()
            progress(60)

            for i, member in enumerate(spec.members):
                check()
                self._create_member(db, spec, member, start=True)
                progress(60 + int(38 * (i + 1) / max(len(spec.members), 1)))
            self._sync_member_rows(db, group, spec)
            group.status, group.error_message = "ready", None
            db.commit()
            self._publish(group, "updated")
            return {"group_id": group_id, "members": len(spec.members)}
        except Exception as e:
            db.rollback()
            group = self.get_group(db, group_id)
            if group is not None and group.status != "deleting":
                group.status, group.error_message = "error", str(e)
                db.commit()
                self._publish(group, "updated")
            raise

    def _ensure_network(self, spec: GroupSpec) -> None:
        name = network_name(spec.name)
        existing = {n["name"]: n for n in libvirt_client.list_networks()}
        if name in existing:
            if not existing[name]["active"]:
                libvirt_client.start_network(name)
            return
        metadata = libvirt_client.group_metadata_xml(spec.name, "network")
        # No <forward> (isolated) and no <ip>: libvirt runs no dnsmasq, the router does DHCP/DNS
        xml = (f"<network><name>{escape(name)}</name><metadata>{metadata}</metadata>"
               f"<bridge stp='on' delay='0'/></network>")
        libvirt_client.create_network(name, xml, autostart=True)

    def _vm_id(self, db: Session, vm_name: str) -> Optional[int]:
        vm = db.query(VM).filter(VM.name == vm_name).first()
        return vm.id if vm else None

    def _create_router(self, db: Session, spec: GroupSpec, start: bool) -> None:
        rendered = get_backend(spec.router.flavour).render(spec)
        image = self.resolve_image(db, spec.router.image)
        nics: List[Tuple[str, Optional[str]]] = []
        if spec.uplink:
            nics.append((spec.uplink, spec.router.uplink_mac))
        nics.append((network_name(spec.name), spec.router.lan_mac))
        vm_service.create_vm(
            db,
            VMCreate(name=router_vm_name(spec.name), description=f"Router of lab group {spec.name}",
                     memory=spec.router.memory, vcpu=spec.router.vcpu, disk_size=spec.router.disk_size,
                     cloud_image_id=image.id, start=start),
            hostname="router", user_data=rendered.user_data, network_config=rendered.network_config, nics=nics,
            metadata_xml=libvirt_client.group_metadata_xml(spec.name, "router", spec=spec.model_dump(mode="json")),
        )

    def _create_member(self, db: Session, spec: GroupSpec, member: MemberSpec, start: bool) -> None:
        ci = member.cloud_init or spec.cloud_init
        image = self.resolve_image(db, member.image)
        vm_service.create_vm(
            db,
            VMCreate(name=member_vm_name(spec.name, member.name), description=f"Member of lab group {spec.name}",
                     memory=member.memory, vcpu=member.vcpu, disk_size=member.disk_size, cloud_image_id=image.id,
                     cloudinit_username=ci.username, cloudinit_password=ci.password, cloudinit_ssh_keys=ci.ssh_keys,
                     cloudinit_keyboard=ci.keyboard, cloudinit_userdata=member.user_data,
                     network_name=network_name(spec.name), mac_address=member.mac, start=start),
            hostname=member.name, fqdn=f"{member.name}.{spec.domain}",
            metadata_xml=libvirt_client.group_metadata_xml(spec.name, member.role, member=member.name),
        )

    def _wait_router(self, vm_name: str, timeout: float, on_wait: Optional[Callable[[], None]] = None) -> None:
        """Wait until the router's first-boot setup is done and dnsmasq runs (through the guest agent)"""
        script = (f"if test -f {STATE_DIR}/ready && systemctl is-active -q dnsmasq; then echo ready; "
                  f"elif test -f {STATE_DIR}/failed; then cat {STATE_DIR}/failed; fi")
        deadline = time.monotonic() + timeout
        last_error = "the guest agent is not responding yet"
        while time.monotonic() < deadline:
            if on_wait:
                on_wait()
            if libvirt_client.agent_ping(vm_name):
                try:
                    out = libvirt_client.agent_exec(vm_name, "/bin/sh", ["-c", script], timeout=30)["stdout"].strip()
                    if out == "ready":
                        return
                    if out:
                        raise RuntimeError(f"Router setup failed:\n{out[-3000:]}")
                    last_error = "cloud-init is still configuring the router"
                except libvirt.libvirtError as e:
                    # guest-exec is disabled until cloud-init reconfigures the agent
                    last_error = e.get_error_message() or str(e)
            time.sleep(5)
        raise TimeoutError(f"Router {vm_name} not ready after {timeout / 60:.0f} min ({last_error}). "
                           "Check its console.")

    # Router config

    def router_config(self, db: Session, group: Group) -> Dict[str, Any]:
        spec = GroupSpec.model_validate(group.spec)
        rendered = get_backend(spec.router.flavour).render(spec)
        return {"flavour": spec.router.flavour, "user_data": rendered.user_data,
                "network_config": rendered.network_config, "files": rendered.files,
                "apply_command": rendered.apply_command, "config_applied": bool(group.config_applied),
                "config_applied_at": group.config_applied_at, "config_error": group.config_error}

    def push_router_config(self, db: Session, group: Group) -> bool:
        """Render the spec and apply it on the router through the guest agent. Returns False
        (and records why) when the router can't be reached; it is applied at the next group start."""
        spec = GroupSpec.model_validate(group.spec)
        vm_name = router_vm_name(spec.name)
        rendered = get_backend(spec.router.flavour).render(spec)
        try:
            libvirt_client.set_domain_group_metadata(vm_name, spec.name, "router", spec=spec.model_dump(mode="json"))
        except libvirt.libvirtError as e:
            logger.warning(f"Could not update {vm_name} metadata: {e}")
        live = libvirt_client.get_vm(vm_name)
        if live is None or live["state"] != "running" or not libvirt_client.agent_ping(vm_name):
            group.config_applied = False
            group.config_error = "Router is not running: the config is applied at the next group start"
            db.commit()
            self._publish(group, "updated")
            return False
        try:
            for path, content in rendered.files.items():
                self._agent_put(vm_name, path, content.encode())
            result = libvirt_client.agent_exec(vm_name, "/bin/sh", ["-c", rendered.apply_command], timeout=60)
            if result["exitcode"] != 0:
                raise RuntimeError(f"apply failed ({result['exitcode']}): {(result['stderr'] or result['stdout']).strip()}")
        except Exception as e:
            group.config_applied, group.config_error = False, f"Could not apply the router config: {e}"
            db.commit()
            self._publish(group, "updated")
            raise RuntimeError(group.config_error)
        group.config_applied, group.config_applied_at, group.config_error = True, _now(), None
        db.commit()
        self._publish(group, "updated")
        return True

    @staticmethod
    def _agent_put(vm_name: str, path: str, data: bytes) -> None:
        """guest-file-write; falls back to `cat > path` through guest-exec (e.g. SELinux denials)"""
        try:
            libvirt_client.agent_write_file(vm_name, path, data)
            return
        except libvirt.libvirtError as e:
            logger.info(f"guest-file-write {path} on {vm_name} failed ({e}), using guest-exec")
        result = libvirt_client.agent_exec(vm_name, "/bin/sh", ["-c", 'cat > "$0"', path], input_data=data)
        if result["exitcode"] != 0:
            raise RuntimeError(f"cannot write {path}: {result['stderr'].strip()}")

    # Update

    def update_group(self, db: Session, group_id: int, new: GroupSpec, replace_members: bool = False,
                     removed_disks: bool = True) -> Optional[Group]:
        group = self.get_group(db, group_id)
        if not group:
            return None
        with self._lock(group.name):
            return self._update(db, group, new, replace_members, removed_disks)

    def _update(self, db: Session, group: Group, new: GroupSpec, replace_members: bool,
                removed_disks: bool) -> Group:
        if group.status in ("creating", "deleting"):
            raise ValueError(f"Group is {group.status}: wait for it to finish")
        old = GroupSpec.model_validate(group.spec)
        for attr in ("name", "cidr", "uplink"):
            if getattr(new, attr) != getattr(old, attr):
                raise ValueError(f"'{attr}' can't be changed after creation (recreate the group)")
        for attr in ("flavour", "image", "memory", "vcpu", "disk_size", "ip", "lan_mac", "uplink_mac"):
            value = getattr(new.router, attr)
            if value is not None and value != getattr(old.router, attr):
                raise ValueError(f"router.{attr} can't be changed after creation (recreate the group)")

        old_members = {m.name: m for m in old.members}
        replaced = []
        for m in new.members:
            prev = old_members.get(m.name)
            if prev is None:
                continue
            changed = [f for f in MEMBER_IMMUTABLE
                       if getattr(m, f) is not None and getattr(m, f) != getattr(prev, f)
                       and not (f == "mac" and m.mac is None)]
            if changed:
                if not replace_members:
                    raise ValueError(f"Member {m.name}: {', '.join(changed)} can't change in place "
                                     "(remove and re-add it, or pass replace_members=true)")
                replaced.append(m.name)
                if "mac" not in changed:
                    m.mac = prev.mac  # keep its MAC (and so its lease) across the rebuild

        spec = self.normalize(db, new, existing=old, group_id=group.id)
        new_names = {m.name for m in spec.members}
        removed = [n for n in old_members if n not in new_names]
        added = [m for m in spec.members if m.name not in old_members]

        router_running = (libvirt_client.get_vm(router_vm_name(spec.name)) or {}).get("state") == "running"
        previous_status = group.status
        group.status = "updating"
        db.commit()
        try:
            for name in removed + replaced:
                self._delete_member_vm(spec.name, name, delete_disks=True if name in replaced else removed_disks)
            group.spec = spec.model_dump(mode="json")
            group.domain = spec.domain
            db.commit()
            # router first, so new members get their lease
            self.push_router_config(db, group)
            for m in added + [m for m in spec.members if m.name in replaced]:
                self._create_member(db, spec, m, start=router_running)
        finally:
            group.status = previous_status
            db.commit()
            vm_service.sync_vms(db)
            self._sync_member_rows(db, group, spec)
            self._publish(group, "updated")
        return group

    def add_member(self, db: Session, group_id: int, member: MemberSpec) -> Optional[Group]:
        group = self.get_group(db, group_id)
        if not group:
            return None
        spec = GroupSpec.model_validate(group.spec)
        if any(m.name == member.name for m in spec.members):
            raise ValueError(f"Member '{member.name}' already exists")
        spec.members.append(member)
        return self.update_group(db, group_id, spec)

    def remove_member(self, db: Session, group_id: int, name: str, delete_disks: bool) -> Optional[Group]:
        group = self.get_group(db, group_id)
        if not group:
            return None
        spec = GroupSpec.model_validate(group.spec)
        if not any(m.name == name for m in spec.members):
            raise LookupError(f"Member '{name}' not found")
        spec.members = [m for m in spec.members if m.name != name]
        return self.update_group(db, group_id, spec, removed_disks=delete_disks)

    def set_dns_record(self, db: Session, group_id: int, record: DNSRecord) -> Optional[Group]:
        group = self.get_group(db, group_id)
        if not group:
            return None
        spec = GroupSpec.model_validate(group.spec)
        spec.router.dns.records = [r for r in spec.router.dns.records if r.name != record.name] + [record]
        return self.update_group(db, group_id, spec)

    def remove_dns_record(self, db: Session, group_id: int, name: str) -> Optional[Group]:
        group = self.get_group(db, group_id)
        if not group:
            return None
        spec = GroupSpec.model_validate(group.spec)
        if not any(r.name == name for r in spec.router.dns.records):
            raise LookupError(f"DNS record '{name}' not found")
        spec.router.dns.records = [r for r in spec.router.dns.records if r.name != name]
        return self.update_group(db, group_id, spec)

    def _delete_member_vm(self, group: str, member: str, delete_disks: bool) -> None:
        vm_name = member_vm_name(group, member)
        meta = libvirt_client.domain_group(vm_name)
        if meta is None:
            return  # gone already, or not ours: never delete a VM outside the group
        if meta.get("name") != group or meta.get("member") != member:
            raise ValueError(f"VM {vm_name} doesn't belong to group {group}: not deleting it")
        libvirt_client.delete_vm(vm_name, delete_disks=delete_disks)

    # Power

    def start_group(self, db: Session, group_id: int) -> Optional[Task]:
        group = self.get_group(db, group_id)
        if not group:
            return None
        if group.status in ("creating", "deleting", "missing"):
            raise ValueError(f"Group is {group.status}")
        return task_service.start(
            db, TaskCreate(name=f"Start lab group {group.name}", type="group_start", target_type="group",
                           target_id=group.id, target_name=group.name),
            self._start_task, group.id)

    def _start_task(self, db: Session, task: Task, group_id: int) -> Dict[str, Any]:
        group = self.get_group(db, group_id)
        spec = GroupSpec.model_validate(group.spec)
        self._ensure_network(spec)
        if spec.uplink and not self._network_active(spec.uplink):
            libvirt_client.start_network(spec.uplink)
        rtr = router_vm_name(spec.name)
        if (libvirt_client.get_vm(rtr) or {}).get("state") != "running":
            libvirt_client.start_vm(rtr)
        task_service.update_progress(db, task.id, 10)
        self._wait_router(rtr, ROUTER_RESTART_TIMEOUT, lambda: None)
        task_service.update_progress(db, task.id, 50)
        with self._lock(group.name):
            self.push_router_config(db, group)
        task_service.update_progress(db, task.id, 60)
        for m in spec.members:
            name = member_vm_name(spec.name, m.name)
            live = libvirt_client.get_vm(name)
            if live and live["state"] == "shutoff":
                libvirt_client.start_vm(name)
            elif live and live["state"] == "paused":
                libvirt_client.resume_vm(name)
        self._publish(group, "updated")
        return {"started": len(spec.members) + 1}

    @staticmethod
    def _network_active(name: str) -> bool:
        return any(n["name"] == name and n["active"] for n in libvirt_client.list_networks())

    def stop_group(self, db: Session, group_id: int, force: bool = False) -> Optional[Task]:
        group = self.get_group(db, group_id)
        if not group:
            return None
        if group.status in ("creating", "deleting"):
            raise ValueError(f"Group is {group.status}")
        return task_service.start(
            db, TaskCreate(name=f"Stop lab group {group.name}", type="group_stop", target_type="group",
                           target_id=group.id, target_name=group.name),
            self._stop_task, group.id, force)

    def _stop_task(self, db: Session, task: Task, group_id: int, force: bool) -> Dict[str, Any]:
        group = self.get_group(db, group_id)
        spec = GroupSpec.model_validate(group.spec)
        members = [member_vm_name(spec.name, m.name) for m in spec.members]
        # members together, router last
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: self._shutdown(n, force), members))
        task_service.update_progress(db, task.id, 70)
        self._shutdown(router_vm_name(spec.name), force)
        self._publish(group, "updated")
        return {"stopped": len(members) + 1}

    @staticmethod
    def _shutdown(vm_name: str, force: bool) -> None:
        live = libvirt_client.get_vm(vm_name)
        if not live or live["state"] == "shutoff":
            return
        if force:
            libvirt_client.stop_vm(vm_name, force=True)
            return
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        try:
            libvirt_client.stop_vm(vm_name)
        except libvirt.libvirtError:
            pass
        while time.monotonic() < deadline:
            time.sleep(2)
            live = libvirt_client.get_vm(vm_name)
            if not live or live["state"] == "shutoff":
                return
        logger.warning(f"{vm_name} ignored ACPI shutdown for {SHUTDOWN_TIMEOUT}s: forcing it off")
        libvirt_client.stop_vm(vm_name, force=True)

    # Delete

    def delete_group(self, db: Session, group_id: int, delete_disks: bool = True) -> bool:
        group = self.get_group(db, group_id)
        if not group:
            return False
        name = group.name
        group.status = "deleting"
        db.commit()
        self._publish(group, "updated")
        with self._lock(name):
            # Only libvirt objects whose metadata says they belong to this group
            for obj in libvirt_client.list_group_objects():
                if obj["group"] != name:
                    continue
                if obj["kind"] == "domain":
                    libvirt_client.delete_vm(obj["name"], delete_disks=delete_disks)
                elif obj["kind"] == "network":
                    libvirt_client.delete_network(obj["name"])
            vm_service.sync_vms(db)
            db.delete(group)
            db.commit()
        event_bus.publish({"kind": "group", "event": "deleted", "id": group_id, "name": name})
        return True

    # libvirt -> DB

    @serialized
    def sync_groups(self, db: Session) -> None:
        """Rebuild / refresh groups from libvirt metadata (router domains carry the spec)"""
        objects = libvirt_client.list_group_objects()
        vm_service.sync_vms(db)
        by_group: Dict[str, List[Dict[str, Any]]] = {}
        for obj in objects:
            by_group.setdefault(obj["group"], []).append(obj)
        groups = {g.name: g for g in db.query(Group).all()}

        for name, objs in by_group.items():
            if name in groups:
                continue
            router = next((o for o in objs if o["kind"] == "domain" and o.get("role") == "router" and o.get("spec")), None)
            if router is None:
                continue
            try:
                spec = GroupSpec.model_validate(router["spec"])
            except ValueError as e:
                logger.warning(f"Ignoring group {name}: invalid spec in {router['name']} metadata ({e})")
                continue
            group = Group(name=spec.name, cidr=spec.cidr, domain=spec.domain, uplink=spec.uplink,
                          router_vm_name=router["name"], spec=spec.model_dump(mode="json"), status="ready",
                          config_applied=True)
            db.add(group)
            db.flush()
            groups[name] = group
            logger.info(f"Rebuilt group {name} from libvirt metadata")

        for name, group in groups.items():
            objs = by_group.get(name, [])
            has_network = any(o["kind"] == "network" for o in objs)
            if group.status == "ready" and not has_network:
                group.status = "missing"
            elif group.status == "missing" and has_network:
                group.status = "ready"
            group.router_vm_id = self._vm_id(db, group.router_vm_name or router_vm_name(name))
            if group.status not in ("creating", "deleting"):
                self._sync_member_rows(db, group, GroupSpec.model_validate(group.spec), commit=False)
        db.commit()

    def _sync_member_rows(self, db: Session, group: Group, spec: GroupSpec, commit: bool = True) -> None:
        rows = {m.name: m for m in group.members}
        wanted = {m.name for m in spec.members}
        for name, row in rows.items():
            if name not in wanted:
                db.delete(row)
        for m in spec.members:
            row = rows.get(m.name)
            if row is None:
                row = GroupMember(group=group, name=m.name)
                db.add(row)
            row.vm_name = member_vm_name(spec.name, m.name)
            row.vm_id = self._vm_id(db, row.vm_name)
            row.role, row.ip, row.mac, row.hostname = m.role, m.ip, m.mac, f"{m.name}.{spec.domain}"
        if commit:
            db.commit()

    # API views

    def to_api(self, db: Session, group: Group, states: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
        spec = GroupSpec.model_validate(group.spec)
        if states is None:
            states = {vm["name"]: vm for vm in libvirt_client.list_vms()}
        vms = {vm.name: vm for vm in db.query(VM).all()}

        def info(name: str, vm_name: str, role: str, ip: Optional[str], mac: Optional[str], m: Any) -> Dict[str, Any]:
            vm = vms.get(vm_name)
            live = states.get(vm_name)
            return {"name": name, "role": role, "hostname": name, "fqdn": f"{name}.{spec.domain}", "ip": ip,
                    "mac": mac, "vm_id": vm.id if vm else None, "vm_name": vm_name,
                    "vm_uuid": live["uuid"] if live else None, "state": live["state"] if live else "missing",
                    "image": m.image, "memory": m.memory, "vcpu": m.vcpu}

        router = info("router", router_vm_name(spec.name), "router", spec.router.ip, spec.router.lan_mac, spec.router)
        members = [info(m.name, member_vm_name(spec.name, m.name), m.role, m.ip, m.mac, m) for m in spec.members]
        all_states = [router["state"]] + [m["state"] for m in members]
        if all(s == "running" for s in all_states):
            state = "running"
        elif all(s in ("shutoff", "missing") for s in all_states):
            state = "stopped"
        else:
            state = "partial"
        network = db.query(Network).filter(Network.name == network_name(spec.name)).first()
        return {
            "id": group.id, "name": group.name, "cidr": spec.cidr, "domain": spec.domain, "uplink": spec.uplink,
            "status": group.status, "state": state, "error_message": group.error_message,
            "network_name": network_name(spec.name), "network_id": network.id if network else None,
            "router": router, "members": members, "member_count": len(members),
            "config_applied": bool(group.config_applied), "config_applied_at": group.config_applied_at,
            "config_error": group.config_error, "spec": spec, "created_at": group.created_at,
            "updated_at": group.updated_at,
        }

    def list_api(self, db: Session) -> List[Dict[str, Any]]:
        groups = self.list_groups(db)
        from app.services.network_service import network_service
        network_service.sync_networks(db)
        states = {vm["name"]: vm for vm in libvirt_client.list_vms()}
        return [self.to_api(db, g, states) for g in groups]

    def detail_api(self, db: Session, group: Group) -> Dict[str, Any]:
        data = self.to_api(db, group)
        spec = data["spec"]
        rtr = router_vm_name(spec.name)
        data["router_uplink_ips"] = [a for i in libvirt_client.get_vm_interfaces(rtr) for a in i["addresses"]]
        data["leases"] = self.leases(group, running=data["router"]["state"] == "running")
        return data

    # Static DHCP reservations (non-member machines) and the router's leases

    def dhcp_hosts(self, group: Group) -> List[DHCPHostSpec]:
        return GroupSpec.model_validate(group.spec).dhcp_hosts

    def set_dhcp_host(self, db: Session, group_id: int, host: DHCPHostSpec,
                      replace_mac: Optional[str] = None) -> Optional[Group]:
        """Add a reservation, or replace the one for replace_mac (applied live on the router)"""
        group = self.get_group(db, group_id)
        if not group:
            return None
        spec = GroupSpec.model_validate(group.spec)
        hosts = spec.dhcp_hosts
        if replace_mac is not None:
            replace_mac = replace_mac.lower()
            if not any(h.mac == replace_mac for h in hosts):
                raise LookupError(f"No reservation for {replace_mac}")
            hosts = [h for h in hosts if h.mac != replace_mac]
        if any(h.mac == host.mac for h in hosts):
            raise ValueError(f"{host.mac} already has a reservation "
                             f"({next(h.ip for h in hosts if h.mac == host.mac)})")
        spec.dhcp_hosts = hosts + [host]
        try:
            spec = GroupSpec.model_validate(spec.model_dump())  # conflicts, with a readable message
        except ValidationError as e:
            raise ValueError("; ".join(err["msg"].replace("Value error, ", "") for err in e.errors()))
        return self.update_group(db, group_id, spec)

    def remove_dhcp_host(self, db: Session, group_id: int, mac: str, release_lease: bool = False) -> Optional[Group]:
        """Remove a reservation; release_lease also drops the MAC's current lease on the router"""
        group = self.get_group(db, group_id)
        if not group:
            return None
        mac = mac.lower()
        spec = GroupSpec.model_validate(group.spec)
        if not any(h.mac == mac for h in spec.dhcp_hosts):
            raise LookupError(f"No reservation for {mac}")
        spec.dhcp_hosts = [h for h in spec.dhcp_hosts if h.mac != mac]
        group = self.update_group(db, group_id, spec)
        if release_lease and any(lease["mac"] == mac for lease in self.leases(group)):
            self.release_lease(db, group_id, mac, force=True)
        return group

    @staticmethod
    def _read_leases(vm_name: str) -> List[Dict[str, Any]]:
        """dnsmasq lease file on the router: '<expiry> <mac> <ip> <hostname|*> <client-id|*>'"""
        out = libvirt_client.agent_exec(vm_name, "/bin/cat", [LEASES_FILE], timeout=5)["stdout"]
        leases = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and ":" in parts[1] and "." in parts[2]:  # IPv4 leases only
                leases.append({"expiry": int(parts[0]), "mac": parts[1].lower(), "ip": parts[2],
                               "hostname": None if parts[3] == "*" else parts[3]})
        return leases

    def leases(self, group: Group, running: Optional[bool] = None) -> List[Dict[str, Any]]:
        """The router's current leases, each marked member / reservation / dynamic, with the VM
        (any VM attached to the group network) that has the MAC. Empty when the router is down."""
        spec = GroupSpec.model_validate(group.spec)
        rtr = router_vm_name(spec.name)
        if running is None:
            running = (libvirt_client.get_vm(rtr) or {}).get("state") == "running"
        if not running:
            return []
        try:
            leases = self._read_leases(rtr)
        except (libvirt.libvirtError, TimeoutError, KeyError, ValueError):
            return []  # agent not ready / no lease file yet
        members = {m.mac: m.name for m in spec.members if m.mac}
        reserved = {h.mac for h in spec.dhcp_hosts}
        vms = self._network_vms(network_name(spec.name))
        for lease in leases:
            mac = lease["mac"]
            lease["kind"] = "member" if mac in members else "reservation" if mac in reserved else "dynamic"
            lease["member"] = members.get(mac)
            vm = vms.get(mac)
            lease["vm_name"] = vm["vm"] if vm else None
            lease["vm_running"] = bool(vm and vm["active"])
            lease["vm_unknown"] = vms is None
        return leases

    @staticmethod
    def _network_vms(network: str) -> Optional[Dict[str, Dict[str, Any]]]:
        """MAC -> {vm, active} for the VMs on a network; None if libvirt can't tell (a domain deleted
        while listing them raises: retry a few times)"""
        for attempt in range(3):
            try:
                return {i["mac"].lower(): i for i in libvirt_client.network_interfaces(network)}
            except libvirt.libvirtError as e:
                logger.info(f"Listing the VMs on {network} failed ({e}), retrying")
                time.sleep(0.2)
        return None

    def release_lease(self, db: Session, group_id: int, mac: str, force: bool = False) -> Optional[Dict[str, Any]]:
        """Drop a lease on the router: stop dnsmasq, delete the MAC's line from its lease file, start it.
        dhcp_release would need dnsmasq-utils on the router (absent on existing routers) and the LAN
        interface name; the restart is the same short blip as every config push.
        Refused while a VM with this MAC is running (it would renew), unless force."""
        group = self.get_group(db, group_id)
        if not group:
            return None
        mac = mac.lower()
        spec = GroupSpec.model_validate(group.spec)
        rtr = router_vm_name(spec.name)
        with self._lock(group.name):
            lease = next((le for le in self.leases(group) if le["mac"] == mac), None)
            if lease is None:
                raise LookupError(f"No DHCP lease for {mac} on the router of group '{spec.name}'")
            logger.debug(f"Release {mac} on {rtr}: lease {lease}, force={force}")
            if lease.pop("vm_unknown") and not force:
                raise RuntimeError("Could not list the VMs on the group network: try again")
            if lease["vm_running"] and not force:
                raise LeaseInUse(f"VM '{lease['vm_name']}' is running with this MAC: it would renew the lease. "
                                 "Stop it first.")
            script = (f"systemctl stop dnsmasq && sed -i '/^[0-9]* {mac} /Id' {LEASES_FILE}; "
                      "systemctl start dnsmasq && systemctl is-active dnsmasq")
            result = libvirt_client.agent_exec(rtr, "/bin/sh", ["-c", script], timeout=60)
            if result["exitcode"] != 0:
                raise RuntimeError(f"Could not release the lease: {(result['stderr'] or result['stdout']).strip()}")
            released = not any(le["mac"] == mac for le in self._read_leases(rtr))
        logger.info(f"Released lease {lease['ip']} ({mac}) on {rtr}: {released}")
        self._publish(group, "updated")
        return {"mac": mac, "ip": lease["ip"], "released": released}

    def export_yaml(self, group: Group) -> Dict[str, Any]:
        spec = GroupSpec.model_validate(group.spec).model_dump(mode="json", exclude_none=True)
        return {"yaml": yaml.safe_dump(spec, sort_keys=False), "spec": spec}


group_service = GroupService()
