"""MetalLB in a lab group, shared by OpenShift (operator add-on) and kubeadm (upstream manifests)

Both run the same configuration and demo, only the way MetalLB is installed and the tool that talks to
the cluster differ (`oc` on the host for OpenShift, `kubectl` on a control plane through the guest agent for
kubeadm: MetalLBClient.kube()).

- l2: the pool is a range of the group network kept free (group spec `address_pools`), one node answers
  ARP for each service IP.
- bgp: the pool is a /27 of BGP_ANNOUNCE_POOL, an announce range of the group router owned by the cluster;
  every node's speaker peers with the router LAN IP (AS 64513 -> 64512) and announces the service IPs
  (ECMP). With BFD on the router, the BGPPeer gets a BFDProfile with the router's timers: a dead node's
  routes are withdrawn in well under a second instead of the 30 s hold time.
- demo: a hello Deployment (2 replicas, answers with its pod and node) + LoadBalancer Service, published
  as hello.<group domain> on the router's DNS.
"""
import ipaddress
import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

NAMESPACE = "metallb-system"
POOL_NAME = "lab-pool"
PEER_NAME = "lab-router"
BFD_PROFILE = "lab-bfd"
DEMO_NAMESPACE = "metallb-demo"
DEMO_NAME = "hello"
# UBI httpd: no Docker Hub rate limit, mirrored for disconnected OpenShift clusters (oc-mirror needs the tag)
DEMO_IMAGE = "registry.access.redhat.com/ubi9/httpd-24:latest"
CRD_TIMEOUT = 5 * 60


def bgp_params(gspec: Any) -> Dict[str, Any]:
    """What the BGPPeer needs from the group router (GroupSpec): its LAN IP, both ASNs, BFD timers"""
    from app.schemas.group import BGP_PEER_ASN
    bgp = gspec.router.bgp
    if bgp is None or not bgp.enabled:
        raise ValueError("BGP is not enabled on the group router")
    bfd = bgp.bfd_on()
    return {"router_ip": gspec.router.ip, "my_asn": bgp.peer_asn or BGP_PEER_ASN, "peer_asn": bgp.asn,
            "bfd": bfd.model_dump(exclude={"enabled"}) if bfd is not None else None}


def config_manifests(pool: str, mode: str, bgp: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], List[List[str]]]:
    """IPAddressPool + L2Advertisement (l2) or BGPPeer (+ BFDProfile) + BGPAdvertisement (bgp).
    Returns (manifests to apply, [kind, name] of the other mode's objects to delete afterwards)."""
    meta = lambda name: {"name": name, "namespace": NAMESPACE}  # noqa: E731
    manifests: List[Dict[str, Any]] = [
        {"apiVersion": "metallb.io/v1beta1", "kind": "IPAddressPool", "metadata": meta(POOL_NAME),
         "spec": {"addresses": [pool], "autoAssign": True, "avoidBuggyIPs": True}},
    ]
    if mode == "bgp":
        if not bgp:
            raise ValueError("MetalLB BGP mode needs the router's BGP settings")
        peer: Dict[str, Any] = {"myASN": int(bgp["my_asn"]), "peerASN": int(bgp["peer_asn"]),
                                "peerAddress": bgp["router_ip"], "holdTime": "30s", "keepaliveTime": "10s"}
        stale = [["l2advertisement", "lab-l2"]]
        bfd = bgp.get("bfd")
        if bfd:
            # same timers as the router: the slower side wins the negotiation anyway
            manifests.append({"apiVersion": "metallb.io/v1beta1", "kind": "BFDProfile", "metadata": meta(BFD_PROFILE),
                              "spec": {"detectMultiplier": int(bfd["detect_multiplier"]),
                                       "receiveInterval": int(bfd["receive_interval"]),
                                       "transmitInterval": int(bfd["transmit_interval"])}})
            peer["bfdProfile"] = BFD_PROFILE
        else:
            stale.append(["bfdprofiles.metallb.io", BFD_PROFILE])  # after the peer stopped using it
        manifests += [
            {"apiVersion": "metallb.io/v1beta2", "kind": "BGPPeer", "metadata": meta(PEER_NAME), "spec": peer},
            {"apiVersion": "metallb.io/v1beta1", "kind": "BGPAdvertisement", "metadata": meta("lab-bgp"),
             "spec": {"ipAddressPools": [POOL_NAME]}},
        ]
    else:
        manifests.append({"apiVersion": "metallb.io/v1beta1", "kind": "L2Advertisement", "metadata": meta("lab-l2"),
                          "spec": {"ipAddressPools": [POOL_NAME]}})
        stale = [["bgpadvertisement", "lab-bgp"], ["bgppeers.metallb.io", PEER_NAME],
                 ["bfdprofiles.metallb.io", BFD_PROFILE]]
    return manifests, stale


def demo_manifests(mode: str, image: str = DEMO_IMAGE) -> List[Dict[str, Any]]:
    """hello Deployment (2 replicas) + LoadBalancer Service of the pool"""
    # the image's docroot isn't writable by OpenShift's random UID: serve the page from an emptyDir
    script = (f'echo "Hello from pod $POD_NAME on node $NODE_NAME (MetalLB {mode.upper()} lab)" '
              "> /var/www/html/index.html; exec run-httpd")
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": DEMO_NAMESPACE}},
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": DEMO_NAME, "namespace": DEMO_NAMESPACE, "labels": {"app": DEMO_NAME}},
         "spec": {"replicas": 2, "selector": {"matchLabels": {"app": DEMO_NAME}},
                  "template": {"metadata": {"labels": {"app": DEMO_NAME}}, "spec": {
                      "containers": [{
                          "name": "httpd", "image": image,
                          "command": ["/bin/sh", "-c", script],
                          "ports": [{"containerPort": 8080}],
                          "env": [{"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
                                  {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}],
                          "readinessProbe": {"httpGet": {"path": "/", "port": 8080}, "periodSeconds": 5},
                          "volumeMounts": [{"name": "docroot", "mountPath": "/var/www/html"}],
                      }],
                      "volumes": [{"name": "docroot", "emptyDir": {}}]}}}},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": DEMO_NAME, "namespace": DEMO_NAMESPACE,
                      "annotations": {"metallb.io/address-pool": POOL_NAME}},
         "spec": {"type": "LoadBalancer", "selector": {"app": DEMO_NAME},
                  "ports": [{"name": "http", "port": 80, "targetPort": 8080}]}},
    ]


# ---------------------------------------------------------------- address pools

def in_pool(ip: str, pool: str) -> bool:
    """pool: "a-b" range or a CIDR"""
    try:
        addr = ipaddress.IPv4Address(ip)
        if "/" in pool:
            return addr in ipaddress.IPv4Network(pool, strict=False)
        start, _, end = pool.partition("-")
        return ipaddress.IPv4Address(start.strip()) <= addr <= ipaddress.IPv4Address((end or start).strip())
    except ValueError:
        return False


def metallb_pool(cidr: str, dhcp_end: str, size: int, taken: List[str]) -> str:
    """Pick `size` consecutive free addresses after the DHCP range (static pool, top of the subnet)"""
    net = ipaddress.IPv4Network(cidr)
    hosts = list(net.hosts())
    end = ipaddress.IPv4Address(dhcp_end)
    used = {ipaddress.IPv4Address(ip) for ip in taken}
    run: List[ipaddress.IPv4Address] = []
    for h in hosts[:-1]:  # keep the last host address free
        if h <= end or h in used:
            run = []
            continue
        run.append(h)
        if len(run) == size:
            return f"{run[0]}-{run[-1]}"
    raise ValueError(f"No {size} free consecutive addresses after the DHCP range in {cidr} for MetalLB")


def pick_l2_pool(gspec: Any, size: int, node_ips: List[str]) -> str:
    taken = [gspec.router.ip] + node_ips + [m.ip for m in gspec.members if m.ip]
    taken += [r.ip for r in gspec.reservations] + [h.ip for h in gspec.dhcp_hosts]
    taken += [r.a for r in gspec.router.dns.records if r.a]
    for p in gspec.address_pools:
        a, b = ipaddress.IPv4Address(p.start), ipaddress.IPv4Address(p.end)
        taken += [str(ipaddress.IPv4Address(i)) for i in range(int(a), int(b) + 1)]
    return metallb_pool(gspec.cidr, gspec.dhcp.end, size, taken)


def assign_pool(network: Any, gspec: Any, mode: str, size: int, node_ips: List[str], cluster_name: str,
                bfd: bool = False) -> str:
    """MetalLB pool, recorded in the group spec (queued on the GroupClusterNetwork, applied by its commit()):
    l2 = a range of the group network kept free; bgp = a /27 outside it, a BGP announce range of the
    router (BGP enabled on the router if needed, BFD too with `bfd`). The other mode's entry is dropped."""
    name = f"{cluster_name}-metallb"
    if mode == "bgp":
        pool = network.free_bgp_range()
        network.remove_address_pool(name)
        network.set_bgp_range(name, pool, bfd=bfd)
        return pool
    pool = pick_l2_pool(gspec, size, node_ips)
    start, end = pool.split("-")
    network.remove_bgp_range(name)
    network.set_address_pool(name, start, end)
    return pool


def demo_hostname(gspec: Any, cluster_name: str) -> str:
    """hello.<domain>, or hello-<cluster>.<domain> when another cluster of the group already has hello"""
    owner = f"cluster:{cluster_name}"
    taken = any(r.name == DEMO_NAME and r.owner != owner for r in gspec.router.dns.records)
    return f"{DEMO_NAME}-{cluster_name}" if taken else DEMO_NAME


# ------------------------------------------------------------------- the cluster side

class MetalLBClient:
    """Configuration, demo and live state through kube(args, timeout, stdin) -> {exitcode, stdout, stderr}
    (oc or kubectl). sleep(s) must raise when the task is cancelled; log(msg) reports progress."""

    sleep: Callable[[float], None] = staticmethod(time.sleep)
    log: Callable[[str], None] = staticmethod(lambda m: None)

    def kube(self, args: List[str], timeout: float = 60, stdin: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def kube_json(self, args: List[str], timeout: float = 60) -> Any:
        out = self.kube(args + ["-o", "json"], timeout)
        if out["exitcode"] != 0:
            raise RuntimeError((out["stderr"] or out["stdout"]).strip()[-500:] or f"{args[0]} failed")
        return json.loads(out["stdout"])

    def apply(self, manifests: List[Dict[str, Any]], timeout: float = 120) -> None:
        import yaml
        text = "\n---\n".join(yaml.safe_dump(m, sort_keys=False) for m in manifests)
        out = self.kube(["apply", "-f", "-"], timeout, stdin=text)
        if out["exitcode"] != 0:
            raise RuntimeError(f"apply failed: {(out['stderr'] or out['stdout']).strip()[-800:]}")

    def apply_retry(self, manifests: List[Dict[str, Any]], what: str, timeout: float = CRD_TIMEOUT) -> None:
        """Apply, retrying while the CRD / webhook isn't served yet"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.apply(manifests)
                return
            except RuntimeError as e:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{what}: {e}")
                self.sleep(10)

    def configure(self, pool: str, mode: str, bgp: Optional[Dict[str, Any]] = None) -> None:
        """Pool + advertisement of `mode`; the other mode's objects are removed"""
        bfd = (bgp or {}).get("bfd") if mode == "bgp" else None
        self.log(f"Configuring MetalLB ({mode.upper()}, pool {pool}"
                 + (f", BFD {bfd['detect_multiplier']} x {bfd['receive_interval']} ms" if bfd else "") + ")")
        manifests, stale = config_manifests(pool, mode, bgp)
        self.apply_retry(manifests, "IPAddressPool / advertisement")
        for kind, name in stale:
            self.kube(["delete", kind, name, "-n", NAMESPACE, "--ignore-not-found"], 60)

    def deploy_demo(self, pool: Optional[str], mode: str, image: str = DEMO_IMAGE, timeout: float = 10 * 60) -> str:
        """hello Deployment + Service; returns the service's external IP. An existing Service whose IP is
        outside `pool` (the pool changed with the mode) is re-created to get an address of the new pool."""
        self.log("Deploying the MetalLB demo (hello)")
        current = self.service_ip()
        if current and pool and not in_pool(current, pool):
            self.log(f"Re-creating the hello Service: {current} is not in the new pool {pool}")
            self.kube(["delete", "svc", DEMO_NAME, "-n", DEMO_NAMESPACE, "--ignore-not-found"], 60)
        self.apply(demo_manifests(mode, image))
        deadline = time.monotonic() + timeout
        while True:
            ip = self.service_ip()
            if ip:
                return ip
            if time.monotonic() > deadline:
                raise TimeoutError("The hello Service got no external IP from MetalLB")
            self.sleep(5)

    def remove_demo(self) -> None:
        self.kube(["delete", "namespace", DEMO_NAMESPACE, "--ignore-not-found", "--wait=false"], 60)

    def service_ip(self) -> Optional[str]:
        out = self.kube(["get", "svc", DEMO_NAME, "-n", DEMO_NAMESPACE, "-o",
                         "jsonpath={.status.loadBalancer.ingress[0].ip}"], 30)
        ip = out["stdout"].strip() if out["exitcode"] == 0 else ""
        return ip or None

    def announcing_node(self) -> Optional[str]:
        """Node whose speaker answers ARP for the demo IP: ServiceL2Status (MetalLB >= 0.14), else the
        latest nodeAssigned event"""
        out = self.kube(["get", "servicel2statuses.metallb.io", "-n", NAMESPACE, "-o", "json"], 30)
        if out["exitcode"] == 0:
            try:
                for item in json.loads(out["stdout"]).get("items", []):
                    st = item.get("status") or {}
                    if st.get("serviceName") == DEMO_NAME and st.get("serviceNamespace") == DEMO_NAMESPACE:
                        return st.get("node")
            except ValueError:
                pass
        out = self.kube(["get", "events", "-n", DEMO_NAMESPACE, "--field-selector",
                         f"involvedObject.name={DEMO_NAME},reason=nodeAssigned", "-o", "json"], 30)
        if out["exitcode"] == 0:
            try:
                events = sorted(json.loads(out["stdout"]).get("items", []),
                                key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
                if events:
                    msg = events[-1].get("message") or ""  # 'announcing from node "x" with protocol "layer2"'
                    if 'node "' in msg:
                        return msg.split('node "', 1)[1].split('"', 1)[0]
            except ValueError:
                pass
        return None

    def demo_endpoints(self) -> List[Dict[str, Any]]:
        out = self.kube(["get", "pods", "-n", DEMO_NAMESPACE, "-l", f"app={DEMO_NAME}", "-o", "json"], 30)
        if out["exitcode"] != 0:
            return []
        result = []
        for pod in json.loads(out["stdout"]).get("items", []):
            ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in (pod.get("status") or {}).get("conditions") or [])
            result.append({"pod": pod["metadata"]["name"], "node": (pod.get("spec") or {}).get("nodeName"),
                           "ip": (pod.get("status") or {}).get("podIP"), "ready": ready})
        return result

    def bfd_profile(self) -> Optional[str]:
        """BFD profile of the BGPPeer, as configured in the cluster"""
        out = self.kube(["get", "bgppeers.metallb.io", PEER_NAME, "-n", NAMESPACE, "-o", "jsonpath={.spec.bfdProfile}"], 30)
        return (out["stdout"].strip() or None) if out["exitcode"] == 0 else None


# ------------------------------------------------------------------- scenario (the MetalLB lab tab)

def scenario(cluster: Any, group: Any, mlb: Dict[str, Any], client: Optional[MetalLBClient],
             router_exec: Callable[[str, float], Dict[str, Any]], ready: bool) -> Dict[str, Any]:
    """State of the MetalLB lab for the diagram + live checks. client = None: the cluster isn't
    reachable (only the stored values are returned)."""
    gspec = (group.spec or {}) if group else {}
    router = gspec.get("router") or {}
    wg = router.get("wireguard") or {}
    mode = mlb.get("mode") or "l2"
    hostname = mlb.get("hostname") or DEMO_NAME
    domain = gspec.get("domain")
    bgp = router.get("bgp") or {}
    bfd = bgp.get("bfd") or {}
    result: Dict[str, Any] = {
        "enabled": bool(mlb.get("enabled")), "mode": mode, "pool": mlb.get("pool"), "service_ip": mlb.get("service_ip"),
        "hostname": f"{hostname}.{domain}" if mlb.get("service_ip") and domain else None,
        "announcing_node": None, "endpoints": [], "router_ip": router.get("ip"), "group_cidr": gspec.get("cidr"),
        "wireguard": bool(wg.get("enabled")), "wireguard_port": wg.get("host_port") if wg.get("enabled") else None,
        "bfd": bool(bgp.get("enabled") and bfd.get("enabled")) if mode == "bgp" else False,
        "state": mlb.get("state"), "message": mlb.get("message"),
        "checks": [],
    }
    if not result["enabled"] or not ready or client is None:
        return result
    checks = result["checks"]
    try:
        ip = client.service_ip()
    except (RuntimeError, ValueError):
        ip = None
    if ip:
        result["service_ip"] = ip
        result["hostname"] = f"{hostname}.{domain}"
    checks.append({"name": "Service got an IP from the pool", "ok": bool(ip),
                   "detail": f"{ip} in {mlb.get('pool')}" if ip else "no external IP (is the demo deployed?)"})
    if not ip:
        return result
    if mode == "bgp":
        _bgp_checks(cluster, group, ip, result, client)
    else:
        result["announcing_node"] = client.announcing_node()
        checks.append({"name": "A node announces it (L2 / ARP)", "ok": bool(result["announcing_node"]),
                       "detail": result["announcing_node"] or "no speaker announces the IP yet"})
    result["endpoints"] = client.demo_endpoints()
    ready_eps = [e for e in result["endpoints"] if e.get("ready")]
    checks.append({"name": "Pods behind the Service", "ok": bool(ready_eps),
                   "detail": f"{len(ready_eps)}/{len(result['endpoints'])} ready"})
    try:
        out = router_exec(f"curl -s -m 5 http://{ip}/", 15)
        ok = out["exitcode"] == 0 and "Hello" in out["stdout"]
        detail = out["stdout"].strip()[:200] if ok else (out["stderr"] or out["stdout"] or "no answer").strip()[:200]
    except Exception as e:  # libvirtError, TimeoutError: router down
        ok, detail = False, str(e)[:200]
    checks.append({"name": f"The router reaches http://{ip}/", "ok": ok, "detail": detail})
    records = (router.get("dns") or {}).get("records") or []
    has_record = any(r.get("name") == hostname and r.get("a") == ip for r in records)
    checks.append({"name": f"DNS {hostname}.{domain} -> {ip}", "ok": has_record,
                   "detail": "served by the router's dnsmasq" if has_record else "record missing"})
    return result


def _bgp_checks(cluster: Any, group: Any, ip: str, result: Dict[str, Any], client: MetalLBClient) -> None:
    """MetalLB BGP: the speakers have an Established session with the router, the router routes the
    service IP to the nodes (one next hop per node, ECMP); with BFD, a BFD session per speaker"""
    from app.services.group_service import group_service
    checks = result["checks"]
    if group is None:
        checks.append({"name": "BGP sessions with the router", "ok": False, "detail": "no lab group"})
        return
    status = group_service.bgp_status(group)
    if status.get("router_error"):
        checks.append({"name": "BGP sessions with the router", "ok": False, "detail": status["router_error"]})
        return
    sessions = {s["peer"]: s for s in status.get("sessions") or []}
    bfd_peers = {p["peer"]: p for p in status.get("bfd_peers") or []}
    peers = [{"node": n.name, "ip": n.ip, "state": (sessions.get(n.ip) or {}).get("state", "no session"),
              "bfd": (bfd_peers.get(n.ip) or {}).get("status") if result.get("bfd") else None}
             for n in cluster.nodes]
    result["bgp_peers"] = peers
    up = [p for p in peers if p["state"] == "Established"]
    checks.append({"name": "Every node has a BGP session with the router", "ok": len(up) == len(peers) and bool(peers),
                   "detail": f"{len(up)}/{len(peers)} Established"
                   + "".join(f"; {p['node']}: {p['state']}" for p in peers if p["state"] != "Established")})
    if result.get("bfd"):
        bfd_up = [p for p in peers if p["bfd"] == "up"]
        profile = client.bfd_profile()
        detect = [p.get("detect_ms") for p in bfd_peers.values() if p.get("detect_ms")]
        checks.append({"name": "BFD watches every session (fast failover)", "ok": len(bfd_up) == len(peers) and bool(profile),
                       "detail": (f"{len(bfd_up)}/{len(peers)} up" + (f", a dead node is detected in {max(detect)} ms" if detect else "")
                                  + ("" if profile else "; the BGPPeer has no BFD profile: switch the mode again to apply it"))})
    route = next((r for r in status.get("routes") or [] if r["prefix"] == f"{ip}/32"), None)
    names = {n.ip: n.name for n in cluster.nodes}
    hops = [names.get(h["ip"], h["ip"]) for h in (route or {}).get("nexthops", [])]
    result["bgp_nexthops"] = hops
    checks.append({"name": f"The router has a BGP route to {ip}", "ok": bool(route and route.get("installed")),
                   "detail": f"via {', '.join(hops)}" + (f" ({len(hops)} next hops, ECMP)" if len(hops) > 1 else "")
                   if route else "no route learned yet"})
