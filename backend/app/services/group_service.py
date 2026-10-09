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
from sqlalchemy.orm.exc import ObjectDeletedError

from app.config import settings
from app.database import serialized
from app.events import event_bus
from app.libvirt_client import libvirt_client
from app.models import CloudImage, Group, GroupMember, Network, Task, VM
from app.schemas import TaskCreate, VMCreate
from app.schemas.group import (
    BGPAnnounceRange, BGPNeighbor, BGPSpec, DHCPHostSpec, DHCPRange, DNSRecord, GroupSpec, MemberSpec,
    WireGuardPeer, WireGuardSpec,
)
from app.services.cloud_image_service import cloud_image_service
from app.services import bgp_service as bgps
from app.services import router_cases
from app.services import wireguard_service as wgs
from app.services.router_service import EL_IMAGES, STATE_DIR, get_backend
from app.services.task_service import task_service
from app.services.vm_service import vm_service

logger = logging.getLogger(__name__)

ROUTER_READY_TIMEOUT = 15 * 60  # first boot installs packages
ROUTER_RESTART_TIMEOUT = 5 * 60
SHUTDOWN_TIMEOUT = 120
LEASES_FILE = "/var/lib/dnsmasq/dnsmasq.leases"

# Member fields that can't change in place (the VM must be recreated)
MEMBER_IMMUTABLE = ("source", "image", "iso", "memory", "vcpu", "disk_size", "role", "mac", "cloud_init", "user_data")


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

    def resolve_iso(self, db: Session, ref: str) -> str:
        """ISO volume path or name -> its path"""
        from app.services.storage_service import storage_service
        isos = storage_service.list_isos(db)
        for iso in isos:
            if ref in (iso["path"], iso["name"]):
                return iso["path"]
        raise ValueError(f"ISO '{ref}' not found (upload or download it on the Storage page)")

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
        # reserved hosts are VMs created by someone else (cluster nodes): their MACs are theirs
        own_macs |= {r.mac for r in spec.reservations}
        taken_macs -= own_macs  # our own VMs' MACs are fine to keep
        router.lan_mac = router.lan_mac or self._new_mac(taken_macs)
        router.uplink_mac = router.uplink_mac or self._new_mac(taken_macs)
        taken_macs |= {router.lan_mac, router.uplink_mac} | {r.mac for r in spec.reservations}

        dhcp_start, dhcp_end = ipaddress.IPv4Address(spec.dhcp.start), ipaddress.IPv4Address(spec.dhcp.end)
        for r in spec.reservations:
            if dhcp_start <= ipaddress.IPv4Address(r.ip) <= dhcp_end:
                raise ValueError(f"Reserved host {r.name} IP {r.ip} is inside the dynamic DHCP range")
        used_ips = {router.ip} | {m.ip for m in spec.members if m.ip} | {r.ip for r in spec.reservations}
        used_ips |= {h.ip for h in spec.dhcp_hosts}
        static_pool = [h for h in hosts[9 if len(hosts) >= 64 else 1:] if not dhcp_start <= h <= dhcp_end
                       and not any(p.contains(str(h)) for p in spec.address_pools)]
        for pool in spec.address_pools:
            for bound in (pool.start, pool.end):
                if ipaddress.IPv4Address(bound) not in net:
                    raise ValueError(f"Address pool {pool.name}: {bound} is outside {net}")
            if not (ipaddress.IPv4Address(pool.end) < dhcp_start or ipaddress.IPv4Address(pool.start) > dhcp_end):
                raise ValueError(f"Address pool {pool.name} overlaps the DHCP range")
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
            if m.source == "cloud_image":
                self.resolve_image(db, m.image)
            elif m.source == "iso":
                self.resolve_iso(db, m.iso)

        reg = spec.router.registry
        if existing is not None:
            old_reg = existing.router.registry
            reg.ca_pem = reg.ca_pem or old_reg.ca_pem
            if reg.enabled and old_reg.disk_gb > reg.disk_gb and (old_reg.enabled or old_reg.hostname):
                raise ValueError(f"The registry disk can't shrink ({old_reg.disk_gb} -> {reg.disk_gb} GiB)")
        if reg.enabled or reg.hostname:
            reg.hostname = f"registry.{spec.domain}"

        subnets = None
        if spec.router.wireguard is not None:
            subnets = [n["cidr"] for n in libvirt_client.network_subnets()]
            wgs.assign(db, spec, existing.router.wireguard if existing else None, group_id, subnets)
        if spec.router.bgp is not None:
            if subnets is None:
                subnets = [n["cidr"] for n in libvirt_client.network_subnets()]
            bgps.assign(db, spec, existing.router.bgp if existing else None, group_id, subnets)

        try:
            spec = GroupSpec.model_validate(spec.model_dump())  # re-run cross-field checks
        except ValidationError as e:  # not the whole spec dump in the API error
            raise ValueError("; ".join(err["msg"].replace("Value error, ", "") for err in e.errors()))
        backend.validate(spec)
        return spec

    def _check_cidr(self, db: Session, net: ipaddress.IPv4Network, group_id: Optional[int]) -> None:
        for other in libvirt_client.network_subnets():
            if net.overlaps(ipaddress.IPv4Network(other["cidr"])):
                raise ValueError(f"{net} overlaps network '{other['network']}' ({other['cidr']})")
        for group in db.query(Group).all():
            # Group networks have no host <ip>, so their subnets are only known here; a "missing"
            # group's network is gone and its subnet is free again
            if group.status == "missing":
                continue
            if group.id != group_id and net.overlaps(ipaddress.IPv4Network(group.cidr)):
                raise ValueError(f"{net} overlaps group '{group.name}' ({group.cidr})")

    # Create

    @staticmethod
    def _nothing_left(group: Group, net_names: set) -> bool:
        """True when none of the group's libvirt objects (network, router, member VMs) exist"""
        if network_name(group.name) in net_names:
            return False
        members = [m.get("name") for m in (group.spec or {}).get("members", [])]
        vm_names = [group.router_vm_name or router_vm_name(group.name)] + [
            member_vm_name(group.name, name) for name in members if name]
        return all(libvirt_client.get_vm(name) is None for name in vm_names)

    def create_group(self, db: Session, spec: GroupSpec) -> Tuple[Group, Task]:
        self.sync_groups(db)
        net_names = {n["name"] for n in libvirt_client.list_networks()}
        existing = db.query(Group).filter(Group.name == spec.name).first()
        if existing is not None:
            if existing.status != "missing" or not self._nothing_left(existing, net_names):
                raise ValueError(f"Group '{spec.name}' already exists")
            # Its network, router and members are all gone (deleted outside this app): the row is
            # only a record of it, so a new group may take the name
            logger.info(f"Replacing missing group {existing.name} (nothing left in libvirt)")
            db.delete(existing)
            db.commit()
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
            self.pin_uplink(db, group)
            self._sync_wg_key(db, group)
            wgs.reconcile(db)
            progress(60)

            for i, member in enumerate(spec.members):
                check()
                self._create_member(db, spec, member, start=True)
                progress(60 + int(38 * (i + 1) / max(len(spec.members), 1)))
            self._sync_member_rows(db, group, spec)
            group.status, group.error_message = "ready", None
            db.commit()
            self._publish(group, "updated")
            result = {"group_id": group_id, "members": len(spec.members)}
            if spec.router.registry.enabled:  # downloads + Quay install: its own task (~5-15 min)
                from app.services import registry_service
                result["registry_task_id"] = registry_service.start_setup(db, group).id
            return result
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
               f"<bridge stp='on' delay='0'/>{router_cases.network_mtu_xml(spec)}</network>")
        libvirt_client.create_network(name, xml, autostart=True)

    @staticmethod
    def _set_network_mtu(spec: GroupSpec) -> None:
        """spec.network.mtu changed: rewrite the libvirt network's <mtu> (saved definition: the bridge and
        the VMs' taps take it at the next group start; DHCP option 26 / the router LAN are live)"""
        import xml.etree.ElementTree as ET
        name = network_name(spec.name)
        try:
            root = ET.fromstring(libvirt_client.get_network_xml(name))
        except libvirt.libvirtError as e:
            logger.warning(f"Could not read network {name}: {e}")
            return
        for old in root.findall("mtu"):
            root.remove(old)
        if spec.network.mtu:
            ET.SubElement(root, "mtu", size=str(spec.network.mtu))
        libvirt_client.redefine_network(ET.tostring(root, encoding="unicode"), restart=False)

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
        registry = spec.router.registry.enabled
        vm_service.create_vm(
            db,
            # the mirror registry needs more RAM / vCPUs than a plain router (spec.router.registry)
            VMCreate(name=router_vm_name(spec.name), description=f"Router of lab group {spec.name}",
                     memory=spec.router.effective_memory(), vcpu=spec.router.effective_vcpu(),
                     disk_size=spec.router.disk_size, cloud_image_id=image.id, start=start and not registry),
            hostname="router", user_data=rendered.user_data, network_config=rendered.network_config, nics=nics,
            metadata_xml=libvirt_client.group_metadata_xml(spec.name, "router", spec=spec.model_dump(mode="json")),
        )
        if registry:
            from app.services import registry_service
            registry_service.ensure_router_disk(spec)
            if start:
                libvirt_client.start_vm(router_vm_name(spec.name))

    def _create_member(self, db: Session, spec: GroupSpec, member: MemberSpec, start: bool) -> None:
        metadata_xml = libvirt_client.group_metadata_xml(spec.name, member.role, member=member.name)
        if member.source != "cloud_image":
            # No cloud-init: the installed OS gets its IP and hostname from the router (dhcp-host on the MAC)
            vm_service.create_vm(
                db,
                VMCreate(name=member_vm_name(spec.name, member.name), description=f"Member of lab group {spec.name}",
                         memory=member.memory, vcpu=member.vcpu, disk_size=member.disk_size,
                         iso_path=self.resolve_iso(db, member.iso) if member.source == "iso" else None,
                         network_name=network_name(spec.name), mac_address=member.mac, start=start),
                metadata_xml=metadata_xml,
            )
            return
        ci = member.cloud_init or spec.cloud_init
        image = self.resolve_image(db, member.image)
        user_data = None
        if not (member.user_data and member.user_data.strip()):
            # egress mode proxy: the member gets the proxy environment (router_cases)
            config = cloud_image_service.build_cloud_config(
                member.name, ci.username, ci.password, [k.strip() for k in ci.ssh_keys if k.strip()], ci.keyboard,
                f"{member.name}.{spec.domain}")
            if router_cases.member_cloud_config(spec, config):
                user_data = "#cloud-config\n" + yaml.safe_dump(config, sort_keys=False)
        vm_service.create_vm(
            db,
            VMCreate(name=member_vm_name(spec.name, member.name), description=f"Member of lab group {spec.name}",
                     memory=member.memory, vcpu=member.vcpu, disk_size=member.disk_size, cloud_image_id=image.id,
                     cloudinit_username=ci.username, cloudinit_password=ci.password, cloudinit_ssh_keys=ci.ssh_keys,
                     cloudinit_keyboard=ci.keyboard, cloudinit_userdata=member.user_data,
                     network_name=network_name(spec.name), mac_address=member.mac, start=start),
            hostname=member.name, fqdn=f"{member.name}.{spec.domain}", metadata_xml=metadata_xml,
            user_data=user_data,
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
            # minutes when an older router first installs haproxy / wireguard-tools
            result = libvirt_client.agent_exec(vm_name, "/bin/sh", ["-c", rendered.apply_command], timeout=300)
            if result["exitcode"] != 0:
                raise RuntimeError(f"apply failed ({result['exitcode']}): {(result['stderr'] or result['stdout']).strip()}")
        except Exception as e:
            group.config_applied, group.config_error = False, f"Could not apply the router config: {e}"
            db.commit()
            self._publish(group, "updated")
            raise RuntimeError(group.config_error)
        group.config_applied, group.config_applied_at, group.config_error = True, _now(), None
        db.commit()
        self._sync_wg_key(db, group)
        self._publish(group, "updated")
        return True

    def _sync_wg_key(self, db: Session, group: Group) -> None:
        """Store the router's WireGuard public key (generated on the router at first use)"""
        spec = GroupSpec.model_validate(group.spec)
        wg = spec.router.wireguard
        if wg is None or not wg.enabled:
            return
        rtr = router_vm_name(spec.name)
        key = wgs.read_router_key(rtr)
        if key is None or key == wg.public_key:
            return
        wg.public_key = key
        group.spec = spec.model_dump(mode="json")
        db.commit()
        try:
            libvirt_client.set_domain_group_metadata(rtr, spec.name, "router", spec=group.spec)
        except libvirt.libvirtError as e:
            logger.warning(f"Could not update {rtr} metadata: {e}")

    @staticmethod
    def _agent_put(vm_name: str, path: str, data: bytes) -> None:
        """guest-file-write; falls back to `cat > path` through guest-exec (e.g. SELinux denials)"""
        try:
            libvirt_client.agent_write_file(vm_name, path, data)
            return
        except libvirt.libvirtError as e:
            logger.info(f"guest-file-write {path} on {vm_name} failed ({e}), using guest-exec")
        result = libvirt_client.agent_exec(vm_name, "/bin/sh", ["-c", 'mkdir -p "$(dirname "$0")" && cat > "$0"', path], input_data=data)
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
        new = self._keep_owned(old, new)
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
        for m in added:  # before touching anything: a failure here would leave a member without VM
            vm_name = member_vm_name(spec.name, m.name)
            if len(vm_name) > 64:
                raise ValueError(f"VM name '{vm_name}' is too long (max 64 characters): use a shorter member name")
            if libvirt_client.get_vm(vm_name) is not None:
                raise ValueError(f"A VM named '{vm_name}' already exists")

        router_running = (libvirt_client.get_vm(router_vm_name(spec.name)) or {}).get("state") == "running"
        previous_status = group.status
        group.status = "updating"
        db.commit()
        self._publish(group, "updated")
        try:
            for name in removed + replaced:
                self._delete_member_vm(spec.name, name, delete_disks=True if name in replaced else removed_disks)
            group.spec = spec.model_dump(mode="json")
            group.domain = spec.domain
            db.commit()
            if spec.network.mtu != old.network.mtu:
                self._set_network_mtu(spec)
            # router first, so new members get their lease
            self.push_router_config(db, group)
            self._registry_changed(db, group, old, spec)
            for m in added + [m for m in spec.members if m.name in replaced]:
                try:
                    self._create_member(db, spec, m, start=router_running)
                except Exception:
                    if m in added:
                        self._drop_member(db, group, spec, m.name)
                    raise
        finally:
            group.status = previous_status
            db.commit()
            vm_service.sync_vms(db)
            self._sync_member_rows(db, group, spec)
            wgs.reconcile(db)
            self._publish(group, "updated")
        return group

    @staticmethod
    def _registry_changed(db: Session, group: Group, old: GroupSpec, new: GroupSpec) -> Optional[Task]:
        """Router resources / registry disk follow spec.router.registry; enabling it starts the setup
        task (which restarts the router when it needs more RAM)"""
        if old.router.registry == new.router.registry and not new.router.registry.enabled:
            return None
        from app.services import registry_service
        registry_service.ensure_router_disk(new)
        reg_old, reg_new = old.router.registry, new.router.registry
        if reg_new.enabled and (not reg_old.enabled or reg_new.port != reg_old.port):
            return registry_service.start_setup(db, group)
        return None

    @staticmethod
    def _keep_owned(old: GroupSpec, new: GroupSpec) -> GroupSpec:
        """A spec sent by a user (UI, API, OpenTofu) can't add, change or drop what the app manages
        (entries with an owner, e.g. a cluster's nodes, API load balancer and records): those are
        taken from the stored spec; the rest comes from the new one."""
        new = new.model_copy(deep=True)
        owned_names = {r.name for r in old.router.dns.records if r.owner}
        new.router.dns.records = ([r for r in new.router.dns.records if not r.owner and r.name not in owned_names]
                                  + [r for r in old.router.dns.records if r.owner])
        new.reservations = ([r for r in new.reservations if not r.owner]
                            + [r for r in old.reservations if r.owner])
        new.load_balancers = ([lb for lb in new.load_balancers if not lb.owner]
                              + [lb for lb in old.load_balancers if lb.owner])
        new.address_pools = ([p for p in new.address_pools if not p.owner]
                             + [p for p in old.address_pools if p.owner])
        new.owner = old.owner
        new.router.uplink_ip = new.router.uplink_ip or old.router.uplink_ip
        # egress / registry blocks left out (older clients) keep the stored ones; the registry's
        # read-back fields are the app's
        given = new.router.model_fields_set
        if "egress" not in given:
            new.router.egress = old.router.egress.model_copy(deep=True)
        if "registry" not in given:
            new.router.registry = old.router.registry.model_copy(deep=True)
        if "path" not in given:
            new.router.path = old.router.path.model_copy(deep=True)
        if "network" not in new.model_fields_set:
            new.network = old.network.model_copy(deep=True)
        if "proxy" not in new.router.egress.model_fields_set:  # squid settings survive mode switches
            new.router.egress.proxy = old.router.egress.proxy.model_copy(deep=True)
        # split DNS zones / resolver knobs: a dns block without them (e.g. OpenTofu's) keeps the stored ones
        dns_given = new.router.dns.model_fields_set
        for attr in ("zones", "stop_rebind", "no_negcache", "cache_size"):
            if attr not in dns_given:
                setattr(new.router.dns, attr, getattr(old.router.dns, attr))
        new.router.registry.hostname = old.router.registry.hostname
        new.router.registry.ca_pem = old.router.registry.ca_pem
        # WireGuard peers are added / removed through /wireguard/peers (devices hold their configs):
        # a spec replacing the group keeps them, and the router's key / assigned subnet and port
        if new.router.wireguard is not None and old.router.wireguard is not None:
            new.router.wireguard.peers = [p.model_copy() for p in old.router.wireguard.peers]
        # BGP: announce ranges / neighbors of a cluster (MetalLB BGP pool) stay, and so does the block
        old_bgp = old.router.bgp
        owned_ranges = [r for r in (old_bgp.announce_ranges if old_bgp else []) if r.owner]
        owned_neighbors = [n for n in (old_bgp.neighbors if old_bgp else []) if n.owner]
        if owned_ranges or owned_neighbors:
            if new.router.bgp is None or not new.router.bgp.enabled:
                owners = sorted({x.owner.split(":", 1)[-1] for x in owned_ranges + owned_neighbors})
                raise ValueError(f"BGP is used by cluster {', '.join(owners)} (MetalLB in BGP mode): "
                                 "it can't be disabled or removed")
        if new.router.bgp is not None and old_bgp is not None:
            # a bgp block without these fields (e.g. OpenTofu's `bgp = true`) keeps the current ones
            given = new.router.bgp.model_fields_set
            if "announce_ranges" not in given:
                new.router.bgp.announce_ranges = [r.model_copy() for r in old_bgp.announce_ranges if not r.owner]
            if "neighbors" not in given:
                new.router.bgp.neighbors = [n.model_copy() for n in old_bgp.neighbors if not n.owner]
        if new.router.bgp is not None:
            new.router.bgp.announce_ranges = ([r for r in new.router.bgp.announce_ranges if not r.owner]
                                              + [r.model_copy() for r in owned_ranges])
            new.router.bgp.neighbors = ([n for n in new.router.bgp.neighbors if not n.owner]
                                        + [n.model_copy() for n in owned_neighbors])
        return new

    # Hosts / records / load balancers managed by the app (cluster nodes in a group, §3.2)

    def update_owned(self, db: Session, group_id: int, mutate: Callable[[GroupSpec], None]) -> Group:
        """Apply `mutate` to the stored spec (it changes reservations / owned records / load
        balancers, never members), validate, save and push the router config live."""
        group = self.get_group(db, group_id)
        if group is None:
            raise ValueError("The cluster's lab group no longer exists")
        with self._lock(group.name):
            db.refresh(group)
            if group.status == "deleting":
                raise ValueError(f"Group {group.name} is being deleted")
            old = GroupSpec.model_validate(group.spec)
            spec = old.model_copy(deep=True)
            mutate(spec)
            spec = GroupSpec.model_validate(spec.model_dump())
            spec = self.normalize(db, spec, existing=old, group_id=group.id)
            group.spec = spec.model_dump(mode="json")
            db.commit()
            wgs.reconcile(db)
            self.push_router_config(db, group)
            self._publish(group, "updated")
            return group

    def free_ip(self, spec: GroupSpec, taken: Any = ()) -> str:
        """A static address (outside the DHCP range) used by nothing in the spec nor in `taken`"""
        net = ipaddress.IPv4Network(spec.cidr)
        hosts = list(net.hosts())
        start, end = ipaddress.IPv4Address(spec.dhcp.start), ipaddress.IPv4Address(spec.dhcp.end)
        used = set(taken) | {spec.router.ip} | {m.ip for m in spec.members if m.ip}
        used |= {r.ip for r in spec.reservations} | {r.a for r in spec.router.dns.records if r.a}
        used |= {h.ip for h in spec.dhcp_hosts}
        for h in hosts[9 if len(hosts) >= 64 else 1:]:
            if not start <= h <= end and str(h) not in used and not any(p.contains(str(h)) for p in spec.address_pools):
                return str(h)
        raise ValueError(f"No free static address left in {net} (outside the DHCP range)")

    def ensure_running(self, db: Session, group_id: int, timeout: float = ROUTER_RESTART_TIMEOUT) -> Group:
        """Network up, router running with its config applied and its uplink address pinned.
        Used by clusters living in the group before they start / add nodes."""
        group = self.get_group(db, group_id)
        if group is None:
            raise ValueError("The cluster's lab group no longer exists")
        if group.status in ("deleting", "missing", "error"):
            raise ValueError(f"Lab group {group.name} is {group.status}"
                             + (f": {group.error_message}" if group.error_message else ""))
        spec = GroupSpec.model_validate(group.spec)
        self._ensure_network(spec)
        if spec.uplink and not self._network_active(spec.uplink):
            libvirt_client.start_network(spec.uplink)
        rtr = router_vm_name(spec.name)
        live = libvirt_client.get_vm(rtr)
        if live is None:
            raise ValueError(f"The router {rtr} of group {group.name} is missing")
        was_running = live["state"] == "running"
        if live["state"] == "paused":
            libvirt_client.resume_vm(rtr)
        elif not was_running:
            libvirt_client.start_vm(rtr)
        self._wait_router(rtr, timeout)
        if not was_running or not group.config_applied:
            with self._lock(group.name):
                self.push_router_config(db, group)
        self.pin_uplink(db, group)
        return group

    def restart_router(self, db: Session, group: Group, timeout: float = ROUTER_RESTART_TIMEOUT) -> None:
        """Clean shutdown + start of the router (new RAM / vCPUs of the saved config), config pushed again.
        The group network has no DHCP / DNS / NAT for the ~1 min it takes."""
        rtr = router_vm_name(group.name)
        logger.info(f"Restarting router {rtr}")
        self._shutdown(rtr, force=False)
        libvirt_client.start_vm(rtr)
        self._wait_router(rtr, timeout)
        with self._lock(group.name):
            self.push_router_config(db, group)
        self.pin_uplink(db, group)

    def pin_uplink(self, db: Session, group: Group, timeout: float = 120) -> Optional[str]:
        """Give the router's uplink NIC a fixed address: its current lease on the uplink network
        becomes a DHCP reservation (so it never changes). Stored as spec.router.uplink_ip. The host
        reaches the router's load balancers (e.g. a cluster API) on that address."""
        spec = GroupSpec.model_validate(group.spec)
        if not spec.uplink or not spec.router.uplink_mac:
            return None
        mac = spec.router.uplink_mac
        rtr = router_vm_name(spec.name)
        ip = spec.router.uplink_ip
        try:
            uplink_xml = libvirt_client.get_network_xml(spec.uplink)
        except libvirt.libvirtError:
            return ip
        import xml.etree.ElementTree as ET
        root = ET.fromstring(uplink_xml)
        has_dhcp = root.find("./ip/dhcp") is not None
        reserved = next((h for h in root.findall("./ip/dhcp/host") if (h.get("mac") or "").lower() == mac), None)
        if reserved is not None and reserved.get("ip"):
            ip = reserved.get("ip")
        else:
            deadline = time.monotonic() + timeout
            while True:
                leases = []
                try:
                    leases = libvirt_client.get_network_leases(spec.uplink) if has_dhcp else []
                except libvirt.libvirtError:
                    pass
                lease = next((le for le in leases if (le.get("mac") or "").lower() == mac
                              and le.get("type", "ipv4") in ("ipv4", 0)), None)
                if lease:
                    ip = lease["ipaddr"]
                    break
                # uplink without libvirt DHCP (host bridge): whatever address the router has
                addrs = [a.split("/")[0] for i in libvirt_client.get_vm_interfaces(rtr)
                         if (i.get("mac") or "").lower() == mac for a in i.get("addresses", [])
                         if ":" not in a]
                if addrs and not has_dhcp:
                    ip = addrs[0]
                    break
                if time.monotonic() > deadline:
                    logger.warning(f"No uplink address for {rtr} after {timeout:.0f}s")
                    return spec.router.uplink_ip
                time.sleep(3)
            if has_dhcp:
                libvirt_client.update_dhcp_host(spec.uplink, "add", mac, ip, rtr)
                logger.info(f"Reserved {ip} for {rtr} on {spec.uplink}")
        if ip != spec.router.uplink_ip:
            spec.router.uplink_ip = ip
            group.spec = spec.model_dump(mode="json")
            db.commit()
            try:
                libvirt_client.set_domain_group_metadata(rtr, spec.name, "router", spec=group.spec)
            except libvirt.libvirtError as e:
                logger.warning(f"Could not update {rtr} metadata: {e}")
            wgs.reconcile(db)  # the WireGuard relay targets the uplink address
            self._publish(group, "updated")
        return ip

    @staticmethod
    def _unpin_uplink(spec: GroupSpec) -> None:
        """Remove the router's reservation from the uplink network (only ours: matching MAC)"""
        if not spec.uplink or not spec.router.uplink_mac:
            return
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(libvirt_client.get_network_xml(spec.uplink))
            for h in root.findall("./ip/dhcp/host"):
                if (h.get("mac") or "").lower() == spec.router.uplink_mac:
                    libvirt_client.update_dhcp_host(spec.uplink, "delete", h.get("mac"), h.get("ip"), h.get("name"))
        except libvirt.libvirtError as e:
            logger.warning(f"Could not remove the uplink reservation of group {spec.name}: {e}")

    def _drop_member(self, db: Session, group: Group, spec: GroupSpec, name: str) -> None:
        """Undo adding a member whose VM could not be created (spec + router config)"""
        spec.members = [m for m in spec.members if m.name != name]
        group.spec = spec.model_dump(mode="json")
        db.commit()
        try:
            self.push_router_config(db, group)
        except RuntimeError as e:
            logger.warning(f"Could not remove member {name} from the router config: {e}")

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

    @staticmethod
    def _check_no_power_task(db: Session, group: Group) -> None:
        """A start racing a stop still waiting on a VM would power that VM off again (and vice versa)"""
        busy = db.query(Task).filter(Task.target_type == "group", Task.target_id == group.id,
                                     Task.type.in_(["group_start", "group_stop"]),
                                     Task.status.in_(["pending", "running"])).first()
        if busy is not None:
            raise ValueError(f"'{busy.name}' is still running: wait for it to finish")

    def start_group(self, db: Session, group_id: int) -> Optional[Task]:
        group = self.get_group(db, group_id)
        if not group:
            return None
        if group.status in ("creating", "deleting", "missing"):
            raise ValueError(f"Group is {group.status}")
        self._check_no_power_task(db, group)
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
        self.pin_uplink(db, group)
        # members, then reserved hosts (cluster nodes: control planes sort before workers)
        for name in ([member_vm_name(spec.name, m.name) for m in spec.members]
                     + sorted((r.name for r in spec.reservations), key=lambda n: "-worker-" in n)):
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
        self._check_no_power_task(db, group)
        return task_service.start(
            db, TaskCreate(name=f"Stop lab group {group.name}", type="group_stop", target_type="group",
                           target_id=group.id, target_name=group.name),
            self._stop_task, group.id, force)

    def _stop_task(self, db: Session, task: Task, group_id: int, force: bool) -> Dict[str, Any]:
        group = self.get_group(db, group_id)
        spec = GroupSpec.model_validate(group.spec)
        # members and reserved hosts (cluster nodes) together, router last
        members = ([member_vm_name(spec.name, m.name) for m in spec.members]
                   + [r.name for r in spec.reservations])
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

    def delete_group(self, db: Session, group_id: int, delete_disks: bool = True,
                     for_cluster: Optional[str] = None) -> bool:
        group = self.get_group(db, group_id)
        if not group:
            return False
        from app.models import Cluster  # clusters live in groups, not the other way round
        clusters = [c.name for c in db.query(Cluster).filter(Cluster.group_id == group.id).all()
                    if c.name != for_cluster]
        if clusters:
            raise ValueError(f"Group {group.name} hosts cluster {', '.join(clusters)}: delete the cluster first")
        name = group.name
        self._unpin_uplink(GroupSpec.model_validate(group.spec))
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
            from app.services import registry_service
            registry_service.forget(name)  # registry credentials / mirror records (the disk went with the router)
        wgs.reconcile(db)  # stops the group's WireGuard relay
        event_bus.publish({"kind": "group", "event": "deleted", "id": group_id, "name": name})
        return True

    # WireGuard remote access (wireguard_service: relay, keys, client configs)

    def set_wireguard(self, db: Session, group_id: int, enabled: bool, listen_port: Optional[int] = None,
                      host_port: Optional[int] = None) -> Optional[Group]:
        """Enable / disable the router's WireGuard (disabled keeps keys and peers)"""
        group = self.get_group(db, group_id)
        if not group:
            return None
        spec = GroupSpec.model_validate(group.spec)
        wg = spec.router.wireguard or WireGuardSpec(enabled=enabled)
        wg.enabled = enabled
        if listen_port:
            wg.listen_port = listen_port
        if host_port:
            wg.host_port = host_port
        spec.router.wireguard = wg
        return self.update_group(db, group_id, spec)

    def add_wg_peer(self, db: Session, group_id: int, name: str, public_key: Optional[str] = None,
                    endpoint_host: Optional[str] = None, allowed_ips: Optional[List[str]] = None) -> Dict[str, Any]:
        """Add a device. Without public_key, generates its key pair: the private key is only in the
        returned config. The router is updated live (wg syncconf)."""
        group = self.get_group(db, group_id)
        if group is None:
            raise LookupError("Group not found")
        with self._lock(group.name):
            db.refresh(group)
            if group.status in ("creating", "deleting"):
                raise ValueError(f"Group is {group.status}: wait for it to finish")
            spec = GroupSpec.model_validate(group.spec)
            wg = spec.router.wireguard
            if wg is None or not wg.enabled:
                raise ValueError("Remote access (WireGuard) is not enabled on this group")
            if any(p.name == name for p in wg.peers):
                raise ValueError(f"A device named '{name}' already exists")
            if not wg.public_key:
                self._sync_wg_key(db, group)
                spec = GroupSpec.model_validate(group.spec)
                wg = spec.router.wireguard
            if not wg.public_key:
                raise ValueError("The router's WireGuard key is not known yet: start the group (or wait until "
                                 "its router is ready) and try again")
            private_key = None
            if not public_key:
                private_key, public_key = wgs.generate_keypair()
            old = spec.model_copy(deep=True)
            wg.peers.append(WireGuardPeer(name=name, public_key=public_key, allowed_ips=allowed_ips or []))
            spec = self.normalize(db, GroupSpec.model_validate(spec.model_dump()), existing=old, group_id=group.id)
            group.spec = spec.model_dump(mode="json")
            db.commit()
            warning = None
            try:
                if not self.push_router_config(db, group):
                    warning = group.config_error
            except RuntimeError as e:
                warning = str(e)
        self._publish(group, "updated")
        peer = next(p for p in spec.router.wireguard.peers if p.name == name)
        host = endpoint_host or wgs.default_endpoint_host()
        return {"peer": peer.model_dump(), "config": wgs.client_config(spec, peer, host, private_key),
                "filename": wgs.config_filename(spec), "has_private_key": private_key is not None,
                "warning": warning}

    def remove_wg_peer(self, db: Session, group_id: int, name: str) -> Optional[Group]:
        group = self.get_group(db, group_id)
        if group is None:
            return None
        wg = GroupSpec.model_validate(group.spec).router.wireguard
        if wg is None or not any(p.name == name for p in wg.peers):
            raise LookupError(f"No WireGuard device named '{name}'")

        def mutate(spec: GroupSpec) -> None:
            spec.router.wireguard.peers = [p for p in spec.router.wireguard.peers if p.name != name]
        return self.update_owned(db, group_id, mutate)

    def wg_peer_config(self, group: Group, name: str, endpoint_host: Optional[str] = None) -> Dict[str, Any]:
        """A device's config again, without its private key (only shown when it was generated)"""
        spec = GroupSpec.model_validate(group.spec)
        wg = spec.router.wireguard
        peer = next((p for p in (wg.peers if wg else []) if p.name == name), None)
        if peer is None:
            raise LookupError(f"No WireGuard device named '{name}'")
        if not wg.public_key:
            raise ValueError("The router's WireGuard key is not known yet: start the group")
        return {"peer": peer.model_dump(), "config": wgs.client_config(spec, peer, endpoint_host or wgs.default_endpoint_host()),
                "filename": wgs.config_filename(spec), "has_private_key": False, "warning": None}

    def wireguard_status(self, group: Group) -> Dict[str, Any]:
        spec = GroupSpec.model_validate(group.spec)
        wg = spec.router.wireguard
        host = wgs.default_endpoint_host()
        if wg is None:
            return {"configured": False, "enabled": False, "endpoint_host": host, "firewall": wgs.host_firewall(),
                    "host_port_range": settings.WG_HOST_PORTS}
        rtr = router_vm_name(spec.name)
        running = (libvirt_client.get_vm(rtr) or {}).get("state") == "running"
        live: Dict[str, Dict[str, Any]] = {}
        router_error = None
        if running and wg.enabled:
            live, router_error = wgs.read_peers(rtr)
        listening, relay_error = wgs.relay.status(wg.host_port) if wg.host_port else (False, None)
        if wg.enabled and not spec.router.uplink_ip:
            relay_error = "The router's uplink address is not known yet (start the group)"
        return {
            "configured": True, "enabled": wg.enabled, "listen_port": wg.listen_port, "host_port": wg.host_port,
            "subnet": wg.subnet, "router_tunnel_ip": wg.router_ip(), "public_key": wg.public_key,
            "endpoint_host": host, "endpoint": f"{host}:{wg.host_port}" if wg.host_port else None,
            "client_allowed_ips": wgs.client_allowed_ips(spec) if wg.subnet else [],
            "relay_listening": listening, "relay_error": relay_error, "firewall": wgs.host_firewall(),
            "host_port_range": settings.WG_HOST_PORTS,
            "router_running": running, "router_error": router_error,
            "peers": [{**p.model_dump(), **live.get(p.public_key, {})} for p in wg.peers],
        }

    # BGP (bgp_service: FRR on the router)

    def set_bgp(self, db: Session, group_id: int, settings_in: Dict[str, Any]) -> Optional[Group]:
        """Enable / disable / configure BGP on the router (applied live). settings_in: the BGPSettings
        fields; None = keep. announce_ranges / neighbors given here replace the user's own ones (those
        owned by a cluster always stay)."""
        group = self.get_group(db, group_id)
        if not group:
            return None
        spec = GroupSpec.model_validate(group.spec)
        bgp = spec.router.bgp or BGPSpec(enabled=settings_in.get("enabled", True))
        bgp.enabled = settings_in.get("enabled", True)
        for key in ("asn", "listen", "maximum_paths"):
            if settings_in.get(key) is not None:
                setattr(bgp, key, settings_in[key])
        if settings_in.get("any_peer_asn"):
            bgp.peer_asn = None
        elif settings_in.get("peer_asn") is not None:
            bgp.peer_asn = settings_in["peer_asn"]
        if settings_in.get("announce_ranges") is not None:
            bgp.announce_ranges = [BGPAnnounceRange.model_validate(r) for r in settings_in["announce_ranges"]]
        if settings_in.get("neighbors") is not None:
            bgp.neighbors = [BGPNeighbor.model_validate(n) for n in settings_in["neighbors"]]
        spec.router.bgp = bgp
        return self.update_group(db, group_id, spec)

    def bgp_status(self, group: Group) -> Dict[str, Any]:
        spec = GroupSpec.model_validate(group.spec)
        rtr = router_vm_name(spec.name)
        running = (libvirt_client.get_vm(rtr) or {}).get("state") == "running"
        return bgps.status(spec, rtr, running)

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
                          config_applied=True, adopted=True)
            db.add(group)
            db.flush()
            groups[name] = group
            logger.info(f"Rebuilt group {name} from libvirt metadata")

        gone = []
        for name, group in groups.items():
            objs = by_group.get(name, [])
            if group.adopted and not objs and group.status in ("ready", "missing"):
                # adopted from libvirt and now entirely gone from it (deleted by whoever created it):
                # nothing of the user's is lost by forgetting it
                gone.append((group.id, name))
                db.delete(group)
                logger.info(f"Forgot group {name}: adopted from libvirt metadata, no longer in libvirt")
                continue
            has_network = any(o["kind"] == "network" for o in objs)
            if group.status == "ready" and not has_network:
                group.status = "missing"
            elif group.status == "missing" and has_network:
                group.status = "ready"
            group.router_vm_id = self._vm_id(db, group.router_vm_name or router_vm_name(name))
            if group.status not in ("creating", "deleting"):
                self._sync_member_rows(db, group, GroupSpec.model_validate(group.spec), commit=False)
        db.commit()
        for group_id, name in gone:
            event_bus.publish({"kind": "group", "event": "deleted", "id": group_id, "name": name, "status": "deleted"})

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
                    "image": m.image or self._source_label(m), "memory": m.memory, "vcpu": m.vcpu}

        router = info("router", router_vm_name(spec.name), "router", spec.router.ip, spec.router.lan_mac, spec.router)
        router.update(memory=spec.router.effective_memory(), vcpu=spec.router.effective_vcpu())  # registry: more
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
            "hosts": [{"name": r.name, "ip": r.ip, "mac": r.mac, "owner": r.owner, "fqdn": f"{r.name}.{spec.domain}",
                       "vm_id": vms[r.name].id if r.name in vms else None,
                       "state": states[r.name]["state"] if r.name in states else "missing"}
                      for r in spec.reservations],
            "clusters": self._clusters_of(db, group),
            "config_applied": bool(group.config_applied), "config_applied_at": group.config_applied_at,
            "config_error": group.config_error, "spec": spec, "created_at": group.created_at,
            "updated_at": group.updated_at,
        }

    @staticmethod
    def _source_label(m: Any) -> Optional[str]:
        """What a non cloud-image member boots, for the members table"""
        if getattr(m, "source", None) == "iso":
            return f"ISO {m.iso.rsplit('/', 1)[-1]}"
        if getattr(m, "source", None) == "empty":
            return "empty disk"
        return None

    def list_api(self, db: Session) -> List[Dict[str, Any]]:
        from app.services.network_service import network_service
        network_service.sync_networks(db)  # first: its commit would expire the group rows listed below
        groups = self.list_groups(db)
        states = {vm["name"]: vm for vm in libvirt_client.list_vms()}
        result = []
        for g in groups:
            try:
                result.append(self.to_api(db, g, states))
            except ObjectDeletedError:
                continue  # deleted by another request meanwhile (e.g. a group delete task finishing)
        return result

    def detail_api(self, db: Session, group: Group) -> Dict[str, Any]:
        data = self.to_api(db, group)
        spec = data["spec"]
        rtr = router_vm_name(spec.name)
        data["router_uplink_ips"] = [a for i in libvirt_client.get_vm_interfaces(rtr) for a in i["addresses"]]
        data["leases"] = self.leases(group, running=data["router"]["state"] == "running")
        data["proxy"] = router_cases.summary(spec) or None
        return data

    @staticmethod
    def _clusters_of(db: Session, group: Group) -> List[Dict[str, Any]]:
        from app.models import Cluster
        return [{"id": c.id, "name": c.name, "type": c.type}
                for c in db.query(Cluster).filter(Cluster.group_id == group.id).order_by(Cluster.name).all()]

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
        reserved = {h.mac for h in spec.dhcp_hosts} | {r.mac for r in spec.reservations}
        vms = self._network_vms(network_name(spec.name)) or {}  # None: network gone (group being deleted)
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
        """The user's part of the spec: what the app manages (cluster nodes, their records and
        load balancers, assigned uplink address) belongs to the cluster, not to the lab"""
        model = GroupSpec.model_validate(group.spec)
        model.router.dns.records = [r for r in model.router.dns.records if not r.owner]
        model.reservations = [r for r in model.reservations if not r.owner]
        model.load_balancers = [lb for lb in model.load_balancers if not lb.owner]
        model.address_pools = [p for p in model.address_pools if not p.owner]
        model.router.uplink_ip = None
        model.router.registry.hostname = model.router.registry.ca_pem = None
        model.owner = None
        if model.router.wireguard is not None:  # assigned on this host / by this router
            wg = model.router.wireguard
            wg.public_key = wg.subnet = wg.host_port = None
            for peer in wg.peers:
                peer.ip = None
        spec = model.model_dump(mode="json", exclude_none=True)
        return {"yaml": yaml.safe_dump(spec, sort_keys=False), "spec": spec}


group_service = GroupService()
