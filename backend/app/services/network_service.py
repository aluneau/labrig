"""Network Service

libvirt is the source of truth; networks are mirrored into the database so
the API can expose stable integer IDs.
"""
import ipaddress
import xml.etree.ElementTree as ET
from typing import List, Optional, Dict, Any
from xml.sax.saxutils import escape, quoteattr

from sqlalchemy.orm import Session

from app.database import serialized
from app.libvirt_client import libvirt_client
from app.models import Network
from app.schemas import NetworkCreate, NetworkUpdate, DHCPHost


class NetworkService:
    """Network management service"""

    @serialized
    def sync_networks(self, db: Session) -> None:
        lv_networks = {n["uuid"]: n for n in libvirt_client.list_networks()}

        for net in db.query(Network).all():
            if net.uuid not in lv_networks:
                db.delete(net)
        db.flush()

        for lv_net in lv_networks.values():
            net = db.query(Network).filter(Network.uuid == lv_net["uuid"]).first()
            if net is None:
                net = Network(uuid=lv_net["uuid"])
                db.add(net)
            net.name = lv_net["name"]
            net.active = lv_net["active"]
            net.autostart = lv_net["autostart"]
            net.persistent = lv_net["persistent"]
            net.xml_config = lv_net["xml"]
            for field, value in self._parse_xml(lv_net["xml"]).items():
                setattr(net, field, value)

        db.commit()

    def list_networks(self, db: Session) -> List[Network]:
        self.sync_networks(db)
        return db.query(Network).order_by(Network.name).all()

    def get_network(self, db: Session, network_id: int) -> Optional[Network]:
        return db.query(Network).filter(Network.id == network_id).first()

    def create_network(self, db: Session, net_data: NetworkCreate) -> Network:
        if db.query(Network).filter(Network.name == net_data.name).first():
            raise ValueError(f"Network with name '{net_data.name}' already exists")

        xml = net_data.xml_config or self._build_network_xml(net_data)
        net_uuid = libvirt_client.create_network(net_data.name, xml, autostart=net_data.autostart)
        self.sync_networks(db)
        return db.query(Network).filter(Network.uuid == net_uuid).first()

    def delete_network(self, db: Session, network_id: int) -> bool:
        net = self.get_network(db, network_id)
        if not net:
            return False
        libvirt_client.delete_network(net.name)
        db.delete(net)
        db.commit()
        return True

    def set_active(self, db: Session, network_id: int, active: bool) -> Optional[Network]:
        net = self.get_network(db, network_id)
        if not net:
            return None
        if active:
            libvirt_client.start_network(net.name)
        else:
            libvirt_client.stop_network(net.name)
        self.sync_networks(db)
        return self.get_network(db, network_id)

    def set_autostart(self, db: Session, network_id: int, autostart: bool) -> Optional[Network]:
        net = self.get_network(db, network_id)
        if not net:
            return None
        libvirt_client.set_network_autostart(net.name, autostart)
        self.sync_networks(db)
        return self.get_network(db, network_id)

    def get_leases(self, db: Session, network_id: int) -> List[Dict[str, Any]]:
        net = self.get_network(db, network_id)
        if not net:
            return []
        return [
            {
                "ip_address": lease["ipaddr"],
                "mac_address": lease["mac"],
                "hostname": lease.get("hostname"),
                "expiry": lease.get("expirytime"),
                "clientid": lease.get("clientid"),
            }
            for lease in libvirt_client.get_network_leases(net.name)
        ]

    # Editing

    def get_config(self, db: Session, network_id: int) -> Optional[Dict[str, Any]]:
        net = self.get_network(db, network_id)
        if not net:
            return None
        xml = libvirt_client.get_network_xml(net.name)
        return {
            "network": {**{c.name: getattr(net, c.name) for c in Network.__table__.columns},
                        "leases": self.get_leases(db, network_id)},
            "hosts": self._dhcp_hosts(xml),
            "interfaces": libvirt_client.network_interfaces(net.name),
            "xml": xml,
        }

    def update_network(self, db: Session, network_id: int, data: NetworkUpdate) -> Optional[Network]:
        """Edit forward mode / domain / subnet / DHCP range in the network's XML.

        Everything else (uuid, bridge, mac, static hosts, DNS...) is preserved.
        """
        net = self.get_network(db, network_id)
        if not net:
            return None
        root = ET.fromstring(libvirt_client.get_network_xml(net.name))

        # forward (kept as-is when unchanged, so custom <nat> options survive)
        current = root.find("forward")
        unchanged = (current is not None and current.get("mode", "nat") == data.forward_mode
                     and (current.get("dev") or None) == (data.forward_dev or None))
        if current is not None and not unchanged:
            root.remove(current)
        if data.forward_mode != "isolated" and not unchanged:
            forward = ET.Element("forward", {"mode": data.forward_mode})
            if data.forward_dev:
                forward.set("dev", data.forward_dev)
            root.insert(2, forward)

        # domain
        for el in root.findall("domain"):
            root.remove(el)
        if data.domain:
            root.append(ET.Element("domain", {"name": data.domain}))

        # IPv4 address + DHCP
        ip = next((el for el in root.findall("ip") if el.get("family", "ipv4") == "ipv4"), None)
        if not data.ip_address:
            if ip is not None:
                root.remove(ip)
        else:
            subnet = self._validate_subnet(data)
            if ip is None:
                ip = ET.SubElement(root, "ip")
            ip.attrib.pop("netmask", None)
            ip.set("address", data.ip_address)
            ip.set("prefix", str(data.prefix))

            dhcp = ip.find("dhcp")
            if dhcp is None:
                dhcp = ET.SubElement(ip, "dhcp")
            for el in dhcp.findall("range"):
                dhcp.remove(el)
            if data.dhcp_enabled:
                hosts = list(subnet.hosts())
                start = data.dhcp_start or str(hosts[1])
                end = data.dhcp_end or str(hosts[-1])
                self._check_in(subnet, start, "DHCP start")
                self._check_in(subnet, end, "DHCP end")
                if ipaddress.ip_address(start) > ipaddress.ip_address(end):
                    raise ValueError("DHCP start must be before DHCP end")
                dhcp.insert(0, ET.Element("range", {"start": start, "end": end}))
            for host in dhcp.findall("host"):
                if host.get("ip") and ipaddress.ip_address(host.get("ip")) not in subnet:
                    raise ValueError(f"Static host {host.get('ip')} is outside the new subnet: remove it first")
            if not len(dhcp):
                ip.remove(dhcp)

        libvirt_client.redefine_network(ET.tostring(root, encoding="unicode"), restart=data.restart)
        self.sync_networks(db)
        return self.get_network(db, network_id)

    def replace_xml(self, db: Session, network_id: int, xml: str, restart: bool) -> Optional[Network]:
        net = self.get_network(db, network_id)
        if not net:
            return None
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            raise ValueError(f"Invalid XML: {e}")
        if root.tag != "network" or root.findtext("name") != net.name:
            raise ValueError(f"XML must be a <network> named '{net.name}' (renaming is not supported)")
        uuid = root.findtext("uuid")
        if uuid and uuid != net.uuid:
            raise ValueError("Changing the network UUID is not allowed")
        libvirt_client.redefine_network(xml, restart=restart)
        self.sync_networks(db)
        return self.get_network(db, network_id)

    def set_dhcp_host(self, db: Session, network_id: int, host: DHCPHost, replace_mac: Optional[str] = None) -> bool:
        """Add a reservation, or replace the one for replace_mac"""
        net = self.get_network(db, network_id)
        if not net:
            return False
        if net.ip_address and net.prefix:
            self._check_in(ipaddress.ip_network(f"{net.ip_address}/{net.prefix}", strict=False), host.ip, "IP")
        existing = self._dhcp_hosts(libvirt_client.get_network_xml(net.name))
        for other in existing:
            if other["mac"].lower() == (replace_mac or "").lower():
                continue
            if other["mac"].lower() == host.mac.lower():
                raise ValueError(f"{host.mac} already has a reservation ({other['ip']})")
            if other["ip"] == host.ip:
                raise ValueError(f"{host.ip} is already reserved for {other['mac']}")
        if replace_mac:
            old = next((h for h in existing if h["mac"].lower() == replace_mac.lower()), None)
            if old is None:
                return False
            libvirt_client.update_dhcp_host(net.name, "delete", old["mac"], old["ip"], old.get("name"))
        libvirt_client.update_dhcp_host(net.name, "add", host.mac.lower(), host.ip, host.name)
        self.sync_networks(db)
        return True

    def delete_dhcp_host(self, db: Session, network_id: int, mac: str) -> bool:
        net = self.get_network(db, network_id)
        if not net:
            return False
        host = next((h for h in self._dhcp_hosts(libvirt_client.get_network_xml(net.name))
                     if h["mac"].lower() == mac.lower()), None)
        if host is None:
            return False
        libvirt_client.update_dhcp_host(net.name, "delete", host["mac"], host["ip"], host.get("name"))
        self.sync_networks(db)
        return True

    @staticmethod
    def _dhcp_hosts(xml: str) -> List[Dict[str, Any]]:
        root = ET.fromstring(xml)
        return [
            {"mac": h.get("mac"), "ip": h.get("ip"), "name": h.get("name")}
            for h in root.findall("./ip/dhcp/host") if h.get("mac") and h.get("ip")
        ]

    @staticmethod
    def _validate_subnet(data: NetworkUpdate):
        if not data.prefix:
            raise ValueError("Prefix is required with an address")
        try:
            address = ipaddress.IPv4Address(data.ip_address)
            subnet = ipaddress.ip_network(f"{data.ip_address}/{data.prefix}", strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid address: {e}")
        if address in (subnet.network_address, subnet.broadcast_address):
            raise ValueError("The host address can't be the network or broadcast address")
        return subnet

    @staticmethod
    def _check_in(subnet, value: str, label: str) -> None:
        try:
            ok = ipaddress.ip_address(value) in subnet
        except ValueError:
            raise ValueError(f"{label} '{value}' is not a valid IP address")
        if not ok:
            raise ValueError(f"{label} {value} is outside {subnet}")

    def _build_network_xml(self, net_data: NetworkCreate) -> str:
        ip_xml = ""
        if net_data.ip_address and net_data.prefix:
            dhcp_xml = ""
            if net_data.dhcp_enabled:
                start, end = net_data.dhcp_start, net_data.dhcp_end
                if not (start and end):
                    hosts = list(ipaddress.ip_network(
                        f"{net_data.ip_address}/{net_data.prefix}", strict=False).hosts())
                    start, end = str(hosts[1]), str(hosts[-1])
                dhcp_xml = f"<dhcp><range start={quoteattr(start)} end={quoteattr(end)}/></dhcp>"
            ip_xml = f"<ip address={quoteattr(net_data.ip_address)} prefix='{int(net_data.prefix)}'>{dhcp_xml}</ip>"

        forward_xml = ""
        if net_data.forward_mode and net_data.forward_mode != "isolated":
            dev_attr = f" dev={quoteattr(net_data.forward_dev)}" if net_data.forward_dev else ""
            forward_xml = f"<forward mode={quoteattr(net_data.forward_mode)}{dev_attr}/>"

        domain_xml = f"<domain name={quoteattr(net_data.domain)}/>" if net_data.domain else ""

        return f"""<network>
            <name>{escape(net_data.name)}</name>
            {forward_xml}
            {domain_xml}
            {ip_xml}
        </network>"""

    def _parse_xml(self, xml: str) -> Dict[str, Any]:
        root = ET.fromstring(xml)
        forward = root.find("forward")
        bridge = root.find("bridge")
        domain = root.find("domain")
        ip = root.find("ip")
        dhcp_range = ip.find("./dhcp/range") if ip is not None else None

        prefix = None
        if ip is not None:
            if ip.get("prefix"):
                prefix = int(ip.get("prefix"))
            elif ip.get("netmask"):
                prefix = ipaddress.ip_network(f"0.0.0.0/{ip.get('netmask')}").prefixlen

        mode = forward.get("mode", "nat") if forward is not None else "isolated"
        return {
            "type": mode,
            "forward_mode": mode,
            "forward_dev": forward.get("dev") if forward is not None else None,
            "bridge_name": bridge.get("name") if bridge is not None else None,
            "domain": domain.get("name") if domain is not None else None,
            "ip_address": ip.get("address") if ip is not None else None,
            "netmask": ip.get("netmask") if ip is not None else None,
            "prefix": prefix,
            "dhcp_enabled": dhcp_range is not None,
            "dhcp_start": dhcp_range.get("start") if dhcp_range is not None else None,
            "dhcp_end": dhcp_range.get("end") if dhcp_range is not None else None,
        }


network_service = NetworkService()
