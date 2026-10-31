"""Render pieces of IPv6 dual stack clusters (docs/ipv6.md "Dual stack clusters"): MetalLB BGP over IPv6 (+ BFD),
the router's FRR config with BFD on the IPv6 sessions, `show bfd peers json` parsing, install-config of a dual stack
platform baremetal cluster."""
from app.schemas.group import GroupSpec
from app.schemas.openshift import OpenShiftOptions
from app.services import bgp_service, metallb
from app.services.openshift_service import openshift_service
from app.services.router_service import ELRouterBackend

PREFIX = "fd00:564d:4d00:61::"


def _spec(bfd=True):
    return GroupSpec.model_validate({
        "name": "e2e-v6d-t", "cidr": "10.61.0.0/24", "domain": "v6d.lab",
        "network": {"ipv6": {"enabled": True, "prefix": f"{PREFIX}/64"}},
        "router": {"ip": "10.61.0.1", "mac": "52:54:00:61:00:01", "lan_mac": "52:54:00:61:00:02",
                   "bgp": {"enabled": True, "listen": True,
                           "announce_ranges": [{"prefix": "10.250.0.0/27", "name": "c-metallb", "owner": "cluster:c"},
                                               {"prefix": "fd00:564d:4d00:62::/64", "name": "c-metallb6",
                                                "owner": "cluster:c"}],
                           **({"bfd": {"enabled": True}} if bfd else {})}},
        "reservations": [{"name": "c-ctlplane-0", "mac": "52:54:00:61:00:10", "ip": "10.61.0.10", "owner": "cluster:c"}],
    })


def test_metallb_bgp_dual_stack_manifests_bfd_on_both_peers():
    gspec = _spec()
    params = metallb.bgp_params(gspec)
    assert params["router_ip6"] == f"{PREFIX}1"
    pool6 = metallb.pool6_for(gspec, "bgp", "10.250.0.0/27", "fd00:564d:4d00:62::/64")
    assert pool6 == "fd00:564d:4d00:62::10-fd00:564d:4d00:62::ff"
    manifests, stale = metallb.config_manifests("10.250.0.0/27", "bgp", params, pool6)
    kinds = {(m["kind"], m["metadata"]["name"]): m for m in manifests}
    assert kinds[("IPAddressPool", "lab-pool")]["spec"]["addresses"] == ["10.250.0.0/27", pool6]
    v4, v6 = kinds[("BGPPeer", "lab-router")]["spec"], kinds[("BGPPeer", "lab-router6")]["spec"]
    assert v4["peerAddress"] == "10.61.0.1" and "ebgpMultiHop" not in v4
    assert v6["peerAddress"] == f"{PREFIX}1" and v6["ebgpMultiHop"] is True
    assert v4["bfdProfile"] == v6["bfdProfile"] == metallb.BFD_PROFILE
    assert ("BFDProfile", metallb.BFD_PROFILE) in kinds
    assert ["l2advertisement", "lab-l2"] in stale and ["bgppeers.metallb.io", "lab-router6"] not in stale
    # back to L2: every BGP object goes, the IPv6 pool = twins of the IPv4 range
    pool6 = metallb.pool6_for(gspec, "l2", "10.61.0.200-10.61.0.207")
    assert pool6 == f"{PREFIX}200-{PREFIX}207"
    _, stale = metallb.config_manifests("10.61.0.200-10.61.0.207", "l2", None, pool6)
    assert ["bgppeers.metallb.io", "lab-router6"] in stale and ["bfdprofiles.metallb.io", "lab-bfd"] in stale


def test_metallb_bgp_dual_stack_without_bfd():
    params = metallb.bgp_params(_spec(bfd=False))
    manifests, stale = metallb.config_manifests("10.250.0.0/27", "bgp", params, "fd00:564d:4d00:62::10-fd00:564d:4d00:62::ff")
    assert all("bfdProfile" not in m["spec"] for m in manifests if m["kind"] == "BGPPeer")
    assert ["bfdprofiles.metallb.io", "lab-bfd"] in stale


def test_frr_bfd_on_ipv6_sessions():
    conf = ELRouterBackend().frr_conf(_spec())
    # IPv4: single-hop static peer, LAB with BFD
    assert " peer 10.61.0.10 local-address 10.61.0.1" in conf
    assert " neighbor LAB bfd profile vmm" in conf
    # IPv6: multihop (the speakers peer with ebgp-multihop from a DHCPv6 /128)
    assert f" peer {PREFIX}10 multihop local-address {PREFIX}1" in conf
    assert " neighbor LAB6 ebgp-multihop 2" in conf
    assert " neighbor LAB6 bfd profile vmm" in conf
    assert "ipv6 prefix-list VMM-ANNOUNCE6 seq 5 permit fd00:564d:4d00:62::/64 le 128" in conf
    # the BFD block stays one block (its checksum decides FRR restarts)
    bfd_block = conf.split("\nbfd\n", 1)[1].split("\nexit\n", 1)[0]
    assert f"{PREFIX}10 multihop" in bfd_block
    # without BFD: no multihop, no BFD lines
    plain = ELRouterBackend().frr_conf(_spec(bfd=False))
    assert "ebgp-multihop" not in plain and "bfd" not in plain


def test_parse_bfd_ipv6_peers():
    data = [
        {"multihop": True, "peer": f"{PREFIX}10", "local": f"{PREFIX}1", "status": "up", "uptime": 12,
         "detect-multiplier": 3, "receive-interval": 200, "transmit-interval": 200,
         "remote-detect-multiplier": 3, "remote-receive-interval": 200, "remote-transmit-interval": 200},
        {"peer": "10.61.0.10", "local": "10.61.0.1", "status": "down", "downtime": 5, "diagnostic": "control detection time expired"},
        {"peer": "garbage"},
    ]
    peers = bgp_service.parse_bfd(data)
    assert [p["peer"] for p in peers] == ["10.61.0.10", f"{PREFIX}10"]
    v6 = peers[1]
    assert v6["status"] == "up" and v6["multihop"] is True and v6["detect_ms"] == 600


def test_dual_stack_baremetal_accepted_and_install_config_vips():
    opts = OpenShiftOptions.model_validate({"topology": "compact", "platform": "baremetal", "dual_stack": True})
    assert opts.dual_stack
    vips = {"api": "10.61.0.5", "ingress": "10.61.0.6", "api6": f"{PREFIX}5", "ingress6": f"{PREFIX}6"}
    config = openshift_service.install_config("c", "v6d.lab", "10.61.0.0/24", 3, 0, "{}", "ssh-ed25519 x",
                                              vips=vips, machine_cidr6=f"{PREFIX}/64")
    bm = config["platform"]["baremetal"]
    assert bm["apiVIPs"] == ["10.61.0.5", f"{PREFIX}5"]
    assert bm["ingressVIPs"] == ["10.61.0.6", f"{PREFIX}6"]
    assert [n["cidr"] for n in config["networking"]["machineNetwork"]] == ["10.61.0.0/24", f"{PREFIX}/64"]
    # IPv4 only: one VIP each
    config = openshift_service.install_config("c", "v6d.lab", "10.61.0.0/24", 3, 0, "{}", "k",
                                              vips={"api": "10.61.0.5", "ingress": "10.61.0.6"})
    assert config["platform"]["baremetal"] == {"apiVIPs": ["10.61.0.5"], "ingressVIPs": ["10.61.0.6"]}
