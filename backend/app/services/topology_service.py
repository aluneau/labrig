"""GET /groups/{id}/topology: one live view of a lab group for the Topology diagram

Aggregates the spec (router, members, reservations, load balancers, pools), libvirt states, the router's
live WireGuard peers and BGP sessions / routes (guest agent), and the clusters living in the group
(nodes, MetalLB pool and service IP, which node answers ARP for it in L2 mode).
"""
import ipaddress
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.libvirt_client import libvirt_client
from app.models import Cluster, Group, VM
from app.schemas.group import GroupSpec
from app.services import bgp_service as bgps

logger = logging.getLogger(__name__)


def _metallb(cluster: Cluster) -> Dict[str, Any]:
    if cluster.type != "openshift":
        return {}
    return ((cluster.spec or {}).get("openshift") or {}).get("metallb") or {}


def _announcing_node(cluster: Cluster) -> Optional[str]:
    """MetalLB L2: the node whose speaker answers ARP for the demo service (oc on the host)"""
    try:
        from app.services.openshift_addons import AddonRunner
        import time
        return AddonRunner(cluster.name, cluster.version, time.sleep, lambda m: None).announcing_node()
    except Exception as e:  # oc missing / API down: the diagram just doesn't mark a node
        logger.info(f"announcing node of {cluster.name}: {e}")
        return None


def _in(ip: str, prefix: str) -> bool:
    try:
        addr, net = ipaddress.ip_address(ip), ipaddress.ip_network(prefix)
        return addr.version == net.version and addr in net
    except ValueError:
        return False


def topology(db: Session, group: Group) -> Dict[str, Any]:
    from app.services.group_service import group_service, member_vm_name, network_name, router_vm_name
    spec = GroupSpec.model_validate(group.spec)
    states = {vm["name"]: vm for vm in libvirt_client.list_vms()}
    base = group_service.to_api(db, group, states)
    vm_ids = {vm.name: vm.id for vm in db.query(VM).all()}
    rtr = router_vm_name(spec.name)
    router_running = base["router"]["state"] == "running"
    clusters = db.query(Cluster).filter(Cluster.group_id == group.id).order_by(Cluster.name).all()
    errors: List[str] = []

    def node_running(c: Cluster) -> bool:
        return bool(c.nodes) and all((states.get(n.name) or {}).get("state") == "running" for n in c.nodes)

    # Live reads from the router / clusters, in parallel (each is a guest-exec or an oc call)
    with ThreadPoolExecutor(max_workers=4) as pool:
        wg_f = pool.submit(group_service.wireguard_status, group)
        bgp_f = pool.submit(bgps.status, spec, rtr, router_running)
        l2_f = {c.name: pool.submit(_announcing_node, c) for c in clusters
                if _metallb(c).get("enabled") and (_metallb(c).get("mode") or "l2") == "l2"
                and _metallb(c).get("service_ip") and c.status == "ready" and node_running(c)}
        try:
            wg = wg_f.result()
        except Exception as e:
            wg, _ = {"configured": False, "enabled": False}, errors.append(f"WireGuard: {e}")
        try:
            bgp = bgp_f.result()
        except Exception as e:
            bgp, _ = {"configured": False}, errors.append(f"BGP: {e}")
        l2 = {name: f.result() for name, f in l2_f.items()}
    if wg.get("router_error"):
        errors.append(wg["router_error"])
    if bgp.get("router_error"):
        errors.append(bgp["router_error"])

    sessions = {s["peer"]: s for s in bgp.get("sessions") or []}
    routes = bgp.get("routes") or []

    machines: List[Dict[str, Any]] = []

    def machine(kind: str, name: str, vm_name: str, ip: Optional[str], mac: Optional[str], role: Optional[str],
                cluster: Optional[str] = None, fqdn: Optional[str] = None) -> Dict[str, Any]:
        ip6 = spec.ip6_of(ip)
        s = sessions.get(ip or "") or sessions.get(ip6 or "")
        m = {"kind": kind, "name": name, "vm_name": vm_name, "vm_id": vm_ids.get(vm_name), "role": role,
             "cluster": cluster, "ip": ip, "ip6": ip6, "mac": mac, "fqdn": fqdn or f"{name}.{spec.domain}",
             "state": (states.get(vm_name) or {}).get("state", "missing"),
             "bgp_state": s["state"] if s else None,
             "bgp_prefixes": [r["prefix"] for r in routes if any(h["ip"] in (ip, ip6) for h in r["nexthops"] if h["ip"])],
             "l2_announces": []}
        machines.append(m)
        return m

    for m in spec.members:
        machine("member", m.name, member_vm_name(spec.name, m.name), m.ip, m.mac, m.role)
    node_cluster = {n.name: (c, n) for c in clusters for n in c.nodes}
    for r in spec.reservations:
        c, n = node_cluster.get(r.name, (None, None))
        machine("node" if c else "reservation", r.name, r.name, r.ip, r.mac, n.role if n else None,
                c.name if c else (r.owner or "").replace("cluster:", "") or None)
    on_net = (group_service._network_vms(network_name(spec.name)) or {}) if spec.dhcp_hosts else {}
    for h in spec.dhcp_hosts:
        vm = (on_net.get(h.mac) or {}).get("vm") or ""
        machine("reservation", h.hostname or h.mac, vm, h.ip, h.mac, None)
    by_name = {m["name"]: m for m in machines}
    by_ip = {m["ip"]: m for m in machines if m["ip"]}
    by_ip.update({m["ip6"]: m for m in machines if m.get("ip6")})

    vips: List[Dict[str, Any]] = []
    cluster_rows = []
    covered = set()
    for c in clusters:
        mlb = _metallb(c)
        mode = (mlb.get("mode") or "l2") if mlb.get("enabled") else None
        ip = mlb.get("service_ip")
        lb_ports = [lb.port for lb in spec.load_balancers if lb.owner == f"cluster:{c.name}"]
        zone = f"{c.name}.{c.domain}"
        api_port = next((lb.port for lb in spec.load_balancers
                         if lb.owner == f"cluster:{c.name}" and lb.name.endswith("-api")), None)
        cluster_rows.append({
            "id": c.id, "name": c.name, "type": c.type, "status": c.status,
            "nodes": [n.name for n in c.nodes],
            "api_url": f"https://{spec.router.uplink_ip}:{api_port}" if api_port and spec.router.uplink_ip else None,
            "console_url": f"https://console-openshift-console.apps.{zone}" if c.type == "openshift" else None,
            "apps_domain": f"apps.{zone}" if c.type == "openshift" else None,
            "load_balancer_ports": sorted(lb_ports), "metallb_enabled": bool(mlb.get("enabled")),
            "metallb_mode": mode, "metallb_pool": mlb.get("pool"), "service_ip": ip,
            "service_hostname": f"hello.{spec.domain}" if ip else None,
        })
        if not ip:
            continue
        if mode == "bgp":
            via = sorted({by_ip[h["ip"]]["name"] for r in routes if _in(ip, r["prefix"])
                          for h in r["nexthops"] if h["ip"] in by_ip})
            covered |= {r["prefix"] for r in routes if r["prefix"] == f"{ip}/32"}
        else:
            node = l2.get(c.name)
            target = next((m for m in machines if node and (m["name"] == node or node.startswith(m["name"] + "."))), None)
            via = [target["name"]] if target else []
            if target:
                target["l2_announces"].append(ip)
        vips.append({"address": ip, "kind": f"metallb-{mode}", "name": "hello",
                     "hostname": f"hello.{spec.domain}", "cluster": c.name, "via": via})
    for r in routes:
        if r["prefix"] in covered:
            continue
        hostname = next((rec.name for rec in spec.router.dns.records
                         if rec.a and r["prefix"] == f"{rec.a}/32"), None)
        vips.append({"address": r["prefix"], "kind": "bgp-route", "name": None,
                     "hostname": f"{hostname}.{spec.domain}" if hostname else None, "cluster": None,
                     "via": [by_ip[h["ip"]]["name"] if h["ip"] in by_ip else h["ip"] for h in r["nexthops"]]})

    wgspec = spec.router.wireguard
    roles = ["dhcp", "dns", "ntp"] + (["ipv6"] if spec.ipv6_prefix() else [])
    if spec.uplink:
        roles.insert(2, "nat")
    if spec.load_balancers:
        roles.append("lb")
    if wgspec is not None and wgspec.enabled:
        roles.append("wireguard")
    if spec.router.bgp is not None and spec.router.bgp.enabled:
        roles.append("bgp")
    if spec.router.registry.enabled:
        roles.append("registry")
    if spec.router.egress.mode == "blocked":
        roles.append("egress")  # lab machines can't reach the internet
    uplink_ips = [a for i in libvirt_client.get_vm_interfaces(rtr) for a in i["addresses"]] if router_running else []
    return {
        "id": group.id, "name": spec.name, "cidr": spec.cidr, "ipv6_prefix": spec.ipv6_prefix(), "domain": spec.domain,
        "network_name": network_name(spec.name), "state": base["state"], "status": group.status,
        "router": {
            "name": rtr, "vm_id": base["router"]["vm_id"], "state": base["router"]["state"],
            "lan_ip": spec.router.ip, "lan_ip6": spec.ip6_of(spec.router.ip),
            "uplink_ip": spec.router.uplink_ip or next((a.split("/")[0] for a in uplink_ips
                                                       if not _in(a.split("/")[0], spec.cidr)), None),
            "uplink_network": spec.uplink, "tunnel_ip": wgspec.router_ip() if wgspec and wgspec.enabled else None,
            "roles": roles, "dhcp_range": f"{spec.dhcp.start} - {spec.dhcp.end}" if spec.dhcp else None,
            "dns_records": len(spec.router.dns.records), "dns_forwarders": spec.router.dns.forwarders,
            "load_balancers": [lb.model_dump() for lb in spec.load_balancers],
            "config_applied": bool(group.config_applied),
        },
        "wireguard": {
            "enabled": bool(wg.get("enabled")), "subnet": wg.get("subnet"), "router_ip": wg.get("router_tunnel_ip"),
            "subnet6": wg.get("subnet6"), "router_ip6": wg.get("router_tunnel_ip6"),
            "host_port": wg.get("host_port"), "listen_port": wg.get("listen_port"), "endpoint": wg.get("endpoint"),
            "relay_listening": bool(wg.get("relay_listening")), "client_allowed_ips": wg.get("client_allowed_ips") or [],
            "peers": [{"name": p["name"], "ip": p.get("ip"), "ip6": p.get("ip6"), "latest_handshake": p.get("latest_handshake"),
                       "endpoint": p.get("endpoint")} for p in wg.get("peers") or []],
        },
        "bgp": bgp, "machines": machines, "clusters": cluster_rows, "vips": vips,
        "address_pools": [p.model_dump() for p in spec.address_pools], "errors": errors,
    }
