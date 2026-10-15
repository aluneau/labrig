"""Emulated BMCs (Redfish) for lab group members, so an ACM hub's metal3 / Ironic can power them and boot them from
virtual media like real servers (docs/openshift.md "Spoke machines (BMC)"). This is an EMULATOR; for REAL
physical servers (the BareMetalHost resource) see baremetal_service.py / bmc.py, which are unrelated.

One sushy-tools process per group with `bmc` members (app/services/bmc_emulator.py, libvirt driver): it listens on
the group's uplink network gateway (e.g. 192.168.122.1, where group machines reach the host through their router's
NAT) on a port of BMC_HOST_PORTS, with HTTP basic auth (credentials per group in DATA_DIR/groups/<g>/bmc-auth.json,
0600, never in the spec / API except GET /groups/{id}/bmc/credentials), and only sees the group's BMC members (VM
names + UUIDs). The process resolves the names under every OpenShift cluster's zone (api, *.apps) to that cluster's
router uplink address, so it can download a hub's discovery ISO (virtual media insert).

Processes are children of the app (stopped with it, restarted by reconcile at startup); reconcile reads the DB
only (VM UUIDs are resolved by the emulator itself).
"""
import hashlib
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Cluster, Group

logger = logging.getLogger(__name__)

USERNAME = "admin"


def available() -> Optional[str]:
    """None when sushy-tools can be imported, else the reason"""
    try:
        import sushy_tools.emulator.main  # noqa: F401
        return None
    except Exception as e:  # ImportError, or a dependency failing on this Python
        return (f"sushy-tools is not installed in the app's venv ({e.__class__.__name__}: {e}); "
                "run scripts/setup.sh again (it pip-installs it)")


def port_range() -> Tuple[int, int]:
    first, _, last = settings.BMC_HOST_PORTS.partition("-")
    return int(first), int(last or first)


def group_dir(name: str) -> Path:
    path = Path(settings.DATA_DIR) / "groups" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bmc_members(spec: Dict[str, Any]) -> List[str]:
    return [m["name"] for m in spec.get("members") or [] if m.get("bmc")]


def _uplink_gateway(network: Optional[str]) -> Optional[str]:
    from app.libvirt_client import libvirt_client
    import ipaddress
    import xml.etree.ElementTree as ET
    if not network:
        return None
    try:
        net = libvirt_client.connect().networkLookupByName(network)
    except Exception:
        return None
    for ip in ET.fromstring(net.XMLDesc(0)).findall("ip"):
        if ip.get("family", "ipv4") == "ipv4" and ip.get("address"):
            return str(ipaddress.ip_address(ip.get("address")))
    return None


def assign(db: Session, spec: Any, existing: Any, group_id: Optional[int]) -> None:
    """spec.bmc (port + listen address) while the group has BMC members (kept once assigned)"""
    from app.schemas.group import BMCSpec
    if not any(m.bmc for m in spec.members):
        spec.bmc = existing.bmc if existing is not None and existing.bmc is not None else None
        return
    bmc = spec.bmc or (existing.bmc if existing is not None else None) or BMCSpec()
    spec.bmc = bmc.model_copy()
    if not spec.bmc.listen:
        spec.bmc.listen = settings.BMC_LISTEN or _uplink_gateway(spec.uplink)
        if not spec.bmc.listen:
            raise ValueError(f"BMC members need the group's uplink network ({spec.uplink}) to have an address on the "
                             "host (a NAT / routed libvirt network), or set BMC_LISTEN")
    if spec.bmc.port is None:
        taken = set()
        for g in db.query(Group).all():
            if g.id != group_id and g.name != spec.name:
                port = ((g.spec or {}).get("bmc") or {}).get("port")
                if port:
                    taken.add(int(port))
        first, last = port_range()
        spec.bmc.port = next((p for p in range(first, last + 1) if p not in taken), None)
        if spec.bmc.port is None:
            raise ValueError(f"No free BMC port left in BMC_HOST_PORTS {settings.BMC_HOST_PORTS}")


def credentials(group_name: str) -> Dict[str, str]:
    """The group's BMC user / password (made on first use), and its htpasswd file for sushy"""
    path = group_dir(group_name) / "bmc-auth.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {"username": USERNAME, "password": secrets.token_urlsafe(12)}
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
    htpasswd = group_dir(group_name) / "bmc-htpasswd"
    if not htpasswd.exists():
        import bcrypt  # a sushy-tools dependency
        line = f"{data['username']}:{bcrypt.hashpw(data['password'].encode(), bcrypt.gensalt()).decode()}\n"
        fd = os.open(str(htpasswd), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(line)
    return data


# Ironic's image server ports on a hub's nodes (host network): forwarded by the hub's router (acm_spokes.ensure_cim)
IRONIC_PORTS = [6180, 6183]


def _redirects(db: Session) -> Dict[str, str]:
    """group CIDR -> its router's uplink address (virtual media URLs pointing at a hub node)"""
    out = {}
    for g in db.query(Group).all():
        spec = g.spec or {}
        uplink = (spec.get("router") or {}).get("uplink_ip")
        if uplink and spec.get("cidr"):
            out[spec["cidr"]] = uplink
    return out


def _hosts(db: Session) -> Dict[str, str]:
    """<cluster>.<domain> -> router uplink address of every cluster in a lab group (hub routes for virtual media)"""
    out = {}
    for c in db.query(Cluster).filter(Cluster.group_id.isnot(None)).all():
        if c.api_ip:
            out[f"{c.name}.{c.domain}"] = c.api_ip
    return out


class Supervisor:
    """The emulator processes: {group name: (Popen, config hash)}"""

    def __init__(self) -> None:
        self._procs: Dict[str, Tuple[subprocess.Popen, str]] = {}
        self._lock = threading.Lock()

    def apply(self, wanted: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            for name in list(self._procs):
                proc, digest = self._procs[name]
                want = wanted.get(name)
                if want is None or want["_hash"] != digest or proc.poll() is not None:
                    self._stop(name)
            for name, config in wanted.items():
                if name in self._procs:
                    continue
                path = group_dir(name) / "bmc-emulator.json"
                path.write_text(json.dumps({k: v for k, v in config.items() if k != "_hash"}))
                log = open(group_dir(name) / "bmc-emulator.log", "ab")
                backend_dir = str(Path(__file__).resolve().parents[2])
                proc = subprocess.Popen([sys.executable, "-m", "app.services.bmc_emulator", str(path)],
                                        cwd=backend_dir, stdout=log, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, start_new_session=True)
                log.close()
                self._procs[name] = (proc, config["_hash"])
                logger.info(f"BMC emulator of group {name}: {config['listen']}:{config['port']} (pid {proc.pid})")

    def running(self, name: str) -> bool:
        with self._lock:
            entry = self._procs.get(name)
            return entry is not None and entry[0].poll() is None

    def _stop(self, name: str) -> None:
        proc, _ = self._procs.pop(name)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop(self) -> None:
        with self._lock:
            for name in list(self._procs):
                self._stop(name)


supervisor = Supervisor()


def reconcile(db: Session) -> None:
    """One emulator per group with BMC members (DB only); allow lists rewritten in place (no restart)"""
    if available() is not None:
        return
    hosts = _hosts(db)
    redirects = _redirects(db)
    wanted: Dict[str, Dict[str, Any]] = {}
    for group in db.query(Group).all():
        spec = group.spec or {}
        names = _bmc_members(spec)
        bmc = spec.get("bmc") or {}
        if group.status in ("deleting", "missing") or not names or not bmc.get("port") or not bmc.get("listen"):
            continue
        from app.services.group_service import member_vm_name
        gdir = group_dir(group.name)
        allow = gdir / "bmc-allow.json"
        vms = sorted(member_vm_name(group.name, n) for n in names)
        if not allow.exists() or json.loads(allow.read_text() or "[]") != vms:
            allow.write_text(json.dumps(vms))
        credentials(group.name)
        state = gdir / "bmc-state"
        state.mkdir(exist_ok=True)
        config = {"listen": bmc["listen"], "port": int(bmc["port"]), "libvirt_uri": settings.LIBVIRT_URI,
                  "auth_file": str(gdir / "bmc-htpasswd"), "allow_file": str(allow), "state_dir": str(state),
                  "hosts": hosts, "redirects": redirects, "redirect_ports": IRONIC_PORTS,
                  "storage_pool": settings.DEFAULT_POOL_NAME}
        config["_hash"] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        wanted[group.name] = config
    supervisor.apply(wanted)


def address(spec: Dict[str, Any]) -> Optional[str]:
    bmc = spec.get("bmc") or {}
    return f"{bmc['listen']}:{bmc['port']}" if bmc.get("listen") and bmc.get("port") else None


def status(group: Group) -> Dict[str, Any]:
    """GET /groups/{id}/bmc: emulator state + one Redfish address per BMC member (no password)"""
    from app.libvirt_client import libvirt_client
    from app.services.group_service import member_vm_name
    spec = group.spec or {}
    names = _bmc_members(spec)
    addr = address(spec)
    members = []
    for name in names:
        vm = member_vm_name(group.name, name)
        uuid = None
        try:
            uuid = libvirt_client.connect().lookupByName(vm).UUIDString()
        except Exception:
            pass
        member = next((m for m in spec.get("members") or [] if m["name"] == name), {})
        members.append({
            "name": name, "vm": vm, "uuid": uuid, "mac": member.get("mac"), "ip": member.get("ip"),
            # +http: the emulator serves plain HTTP, Ironic defaults to https for redfish-virtualmedia://
            "redfish_url": f"redfish-virtualmedia+http://{addr}/redfish/v1/Systems/{uuid}" if addr and uuid else None,
        })
    problem = available()
    return {"enabled": bool(names), "address": addr, "running": supervisor.running(group.name),
            "username": USERNAME if names else None, "members": members, "error": problem,
            "firewall": (f"The host firewall must let the group machines reach {addr}/tcp: scripts/setup.sh opens "
                         f"BMC_HOST_PORTS ({settings.BMC_HOST_PORTS}) on the libvirt bridges") if addr else None}
