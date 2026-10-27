"""Host-side DNS resolver for groupless / bare-metal OpenShift clusters.

A bare-metal OpenShift cluster has no lab-group router, so nothing resolves its api / *.apps names. This
runs a small dnsmasq on the vm-manager host (which every lab network can reach) that answers, per such
cluster, api / api-int.<cluster>.<domain> and *.apps.<cluster>.<domain> -> the cluster's node IP, and
forwards everything else to the host's own upstream resolver. Point a client (or a bare-metal node, or a
future ACM spoke) at the host's address to reach those clusters without a router or a VPN.

The host address is detected at run time (the source IP the host uses to reach the lab) — never hardcoded:
"wherever vm-manager runs". dnsmasq runs as the `vm-manager-dns` systemd unit reading DATA_DIR/dns/dnsmasq.conf
(written here, app-owned); the app restarts it with `systemctl` (allowed for this unit by the polkit rule,
like the libvirt units). Disabled or a no-op when the unit isn't installed (setup.sh installs it).
"""
import logging
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Cluster, HostDnsRecord

logger = logging.getLogger(__name__)

UNIT = "vm-manager-dns.service"
SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
# A lab address only used to learn the host's own primary IP (no packet is sent for UDP connect)
_PROBE = "10.255.255.255"


class DNSService:
    @property
    def _dir(self) -> Path:
        path = Path(settings.DATA_DIR) / "dns"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def _conf(self) -> Path:
        return self._dir / "dnsmasq.conf"

    # ------------------------------------------------------------------ host facts

    def host_ip(self) -> Optional[str]:
        """The host's primary LAN IP (the source address it reaches the lab upstream / a route from).
        Detected at run time so nothing is hardcoded to a particular host."""
        target = (self._upstreams() or [_PROBE])[0]
        for dest in (target, _PROBE, "8.8.8.8"):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((dest, 53))
                ip = s.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
            except OSError:
                continue
            finally:
                s.close()
        return None

    @staticmethod
    def _upstreams() -> List[str]:
        """The host's real upstream nameservers (skip systemd-resolved's 127.0.0.53 stub)."""
        servers: List[str] = []
        for path in ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"):
            try:
                for line in open(path):
                    m = re.match(r"\s*nameserver\s+(\S+)", line)
                    if m and ":" not in m.group(1) and not m.group(1).startswith("127."):
                        if m.group(1) not in servers:
                            servers.append(m.group(1))
            except OSError:
                continue
            if servers:
                break
        return servers

    # ------------------------------------------------------------------ records

    @staticmethod
    def _clusters(db: Session) -> List[Cluster]:
        """OpenShift clusters with no lab-group router but a routable node IP: bare-metal (and groupless
        ACM spokes). The group-based clusters get their DNS from their router instead."""
        return [c for c in db.query(Cluster).filter(Cluster.type == "openshift").all()
                if c.group_id is None and c.api_ip]

    @staticmethod
    def _vm_hub_zones(db: Session) -> List[Cluster]:
        """VM-based ACM hubs (in a lab group, so normally only resolvable inside the group) that provision a
        REAL bare-metal spoke. The physical BMC + the groupless spoke node sit on a routable network and must
        resolve the hub's api / *.apps by name to fetch the discovery ISO (assisted-image-service) and join;
        they can't reach the group router's internal DNS. Publishing the hub zone here -> hub.api_ip (the
        router's uplink address, where haproxy already fronts the hub API 6443 and ingress 80/443) gives them
        a routable name to resolve. A bare-metal (groupless) hub needs none of this: it is already in
        _clusters()."""
        clusters = db.query(Cluster).filter(Cluster.type == "openshift").all()
        real_spoke_hub_ids = set()
        for c in clusters:
            prov = ((c.spec or {}).get("openshift") or {}).get("provisioner") or {}
            if prov.get("kind") == "acm" and prov.get("host_id") is not None and prov.get("hub_id") is not None:
                real_spoke_hub_ids.add(prov["hub_id"])
        return [c for c in clusters
                if c.group_id is not None and c.api_ip and c.id in real_spoke_hub_ids
                and ((c.spec or {}).get("openshift") or {}).get("acm")]

    def zones(self, db: Session) -> List[Dict[str, Any]]:
        out = []
        for c in self._clusters(db):
            zone = f"{c.name}.{c.domain}"
            # A multi-node spoke runs an on-cluster load balancer (keepalived) on two VIPs: api -> apiVIP,
            # *.apps -> ingressVIP. Single node (SNO) / the hub: one address for both.
            apps_ip = ((c.spec or {}).get("apps_ip")) or c.api_ip
            out.append({"cluster": c.name, "zone": zone, "ip": c.api_ip, "apps_ip": apps_ip})
        for c in self._vm_hub_zones(db):
            zone = f"{c.name}.{c.domain}"
            # A physical spoke is outside the hub's libvirt network.  Publishing the hub/router's
            # private uplink address here makes discovery resolve successfully but time out on HTTPS.
            # The vm-manager host fronts hub API/ingress on its LAN address, which is the address a
            # real BMC and its discovery ISO can route to.
            host_ip = self.host_ip()
            out.append({"cluster": c.name, "zone": zone, "ip": host_ip or c.api_ip,
                        "apps_ip": host_ip or c.api_ip})
        return out

    # ------------------------------------------------------------------ custom records

    @staticmethod
    def _norm(name: str) -> str:
        return name.strip().rstrip(".").lower()

    def owns(self, db: Session, name: str) -> Optional[str]:
        """The cluster whose zone already owns `name` (api / api-int / *.apps of a bare-metal cluster),
        or None. A custom record must not shadow one the resolver generates itself."""
        name = self._norm(name)
        for z in self.zones(db):
            zone = z["zone"].lower()
            if name in ("api." + zone, "api-int." + zone):
                return z["cluster"]
            if name == "apps." + zone or name.endswith(".apps." + zone):
                return z["cluster"]
        return None

    def custom_records(self, db: Session) -> List[Dict[str, Any]]:
        """User-managed records, newest first, as {name, ip, cname, type}."""
        rows = db.query(HostDnsRecord).order_by(HostDnsRecord.name).all()
        out = []
        for r in rows:
            out.append({"name": r.name, "ip": r.ip, "cname": r.cname,
                        "type": "A" if r.ip else "CNAME"})
        return out

    def add_record(self, db: Session, name: str, ip: Optional[str] = None,
                   cname: Optional[str] = None) -> Dict[str, Any]:
        """Add a custom A record (name -> ip) or alias (name -> cname). Rejects (ValueError) a name that
        collides with a cluster zone the resolver already owns, or one that already exists. Persists and
        re-renders / restarts the resolver."""
        name = self._norm(name)
        cname = self._norm(cname) if cname else None
        owner = self.owns(db, name)
        if owner:
            raise ValueError(f"'{name}' is already resolved by the host for cluster '{owner}'")
        if db.query(HostDnsRecord).filter(HostDnsRecord.name == name).first():
            raise ValueError(f"a record for '{name}' already exists")
        if cname:
            self._check_cname_target(db, name, cname)
        row = HostDnsRecord(name=name, ip=ip, cname=cname)
        db.add(row)
        db.commit()
        self.reconcile(db)
        return {"name": row.name, "ip": row.ip, "cname": row.cname,
                "type": "A" if row.ip else "CNAME"}

    def _check_cname_target(self, db: Session, name: str, target: str) -> None:
        """dnsmasq's cname= only resolves to an address when the target is a name dnsmasq knows itself
        (host-record / another cname): an upstream name (example.com) or one only covered by a wildcard
        (address=) answers the bare CNAME and clients get no address."""
        if target == name:
            raise ValueError(f"'{name}' can't be an alias of itself")
        local = {r.name for r in db.query(HostDnsRecord).all() if not r.name.startswith("*.")}
        for z in self.zones(db):
            local.update(("api." + z["zone"].lower(), "api-int." + z["zone"].lower()))
        if target not in local:
            raise ValueError(
                f"the alias target '{target}' must be a name this resolver serves itself (a custom A record or "
                "alias, or a cluster's api / api-int name): dnsmasq can't follow an alias to an upstream name or a "
                "wildcard, clients would get no address. Add an A record instead.")

    def delete_record(self, db: Session, name: str) -> bool:
        """Remove a custom record by name. Returns False if it did not exist."""
        name = self._norm(name)
        row = db.query(HostDnsRecord).filter(HostDnsRecord.name == name).first()
        if not row:
            return False
        db.delete(row)
        db.commit()
        self.reconcile(db)
        return True

    def render(self, db: Session, host_ip: str) -> str:
        lines = [
            "# Generated by vm-manager (services/dns_service.py) — do not edit",
            # Bind only the host's LAN IP (every lab network reaches it); leaves systemd-resolved's
            # 127.0.0.53 stub alone.
            "port=53", f"listen-address={host_ip}", "bind-interfaces", "no-resolv", "no-hosts",
            "log-queries=no",
        ]
        for up in self._upstreams():
            lines.append(f"server={up}")
        for z in self.zones(db):
            lines.append(f"# cluster {z['cluster']}")
            lines.append(f"host-record=api.{z['zone']},{z['ip']}")
            lines.append(f"host-record=api-int.{z['zone']},{z['ip']}")
            lines.append(f"address=/apps.{z['zone']}/{z.get('apps_ip') or z['ip']}")
        custom = self.custom_records(db)
        if custom:
            lines.append("# custom records (user-managed, services/dns_service.py)")
            for r in custom:
                # Skip anything a cluster zone now owns (shouldn't happen: add_record rejects it)
                if self.owns(db, r["name"]):
                    continue
                if r["ip"] and r["name"].startswith("*."):
                    # host-record takes no wildcard: address=/zone/ip answers the zone and every name below it
                    lines.append(f"address=/{r['name'][2:]}/{r['ip']}")
                elif r["ip"]:
                    # host-record also answers the matching PTR
                    lines.append(f"host-record={r['name']},{r['ip']}")
                elif r["cname"]:
                    lines.append(f"cname={r['name']},{r['cname']}")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ unit

    def available(self) -> bool:
        """The systemd unit is installed (setup.sh), so we can manage the resolver."""
        systemctl = shutil.which("systemctl", path=SYSTEM_PATH)
        if not systemctl:
            return False
        out = subprocess.run([systemctl, "list-unit-files", UNIT], capture_output=True, text=True)
        return out.returncode == 0 and UNIT in out.stdout

    def _restart(self) -> None:
        systemctl = shutil.which("systemctl", path=SYSTEM_PATH)
        if not systemctl:
            return
        # Allowed for this unit by scripts/polkit/50-vm-manager.rules (like the libvirt units)
        subprocess.run([systemctl, "restart", UNIT], capture_output=True, text=True, timeout=30)

    def reconcile(self, db: Session) -> None:
        """Rewrite the resolver config from the DB and restart it. Called at startup and whenever a
        bare-metal cluster is created / deleted / becomes ready. No-op when disabled or not installed."""
        if not settings.HOST_DNS_ENABLED:
            return
        host_ip = self.host_ip()
        if not host_ip:
            logger.warning("Host DNS: could not detect the host's primary IP; skipping")
            return
        try:
            text = self.render(db, host_ip)
            fd = os.open(self._conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(text)
        except OSError as e:
            logger.warning(f"Host DNS: could not write {self._conf}: {e}")
            return
        if self.available():
            try:
                self._restart()
            except Exception as e:  # never let DNS break a create / startup
                logger.warning(f"Host DNS: could not restart {UNIT}: {e}")

    def status(self, db: Session) -> Dict[str, Any]:
        systemctl = shutil.which("systemctl", path=SYSTEM_PATH)
        running = False
        if systemctl and self.available():
            out = subprocess.run([systemctl, "is-active", UNIT], capture_output=True, text=True)
            running = out.stdout.strip() == "active"
        return {"enabled": bool(settings.HOST_DNS_ENABLED), "installed": self.available(), "running": running,
                "host_ip": self.host_ip(), "upstreams": self._upstreams(), "zones": self.zones(db),
                "records": self.custom_records(db)}


dns_service = DNSService()
