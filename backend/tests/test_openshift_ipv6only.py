"""IPv6-only OpenShift (openshift.ip_family ipv6, docs/ipv6.md "IPv6-only OpenShift"): options, install-config,
agent-config, router render (IPv6-only reservations, IPv6 load balancer backends, registry IPv6 front, NTP)"""
import pytest
from pydantic import ValidationError

from app.schemas.group import GroupSpec, LoadBalancerSpec
from app.schemas.openshift import OpenShiftOptions
from app.services import registry_router
from app.services.openshift_service import openshift_service
from app.services.router_service import ELRouterBackend

PREFIX = "fd00:1:2:3::/64"


def group(**extra) -> GroupSpec:
    spec = {
        "name": "g", "cidr": "10.62.0.0/24", "network": {"ipv6": {"enabled": True, "prefix": PREFIX}},
        "router": {"ip": "10.62.0.1", "lan_mac": "52:54:00:00:00:01", "uplink_mac": "52:54:00:00:00:02",
                   "uplink_ip": "192.168.122.50", "registry": {"enabled": True}},
        "dhcp": {"start": "10.62.0.100", "end": "10.62.0.200"},
        "reservations": [{"name": "c-master-0", "mac": "52:54:00:00:00:10", "ip": "10.62.0.10",
                          "ipv6_only": True, "owner": "cluster:c"},
                         {"name": "other", "mac": "52:54:00:00:00:11", "ip": "10.62.0.11"}],
        "load_balancers": [{"name": "c-api", "port": 6443, "backends": ["[fd00:1:2:3::10]:6443"],
                            "owner": "cluster:c"}],
    }
    spec.update(extra)
    return GroupSpec.model_validate(spec)


# options

def test_ip_family_defaults_follow_dual_stack():
    assert OpenShiftOptions().ip_family == "ipv4"
    assert OpenShiftOptions(dual_stack=True).ip_family == "dual_stack"
    opts = OpenShiftOptions(ip_family="dual_stack")
    assert opts.dual_stack is True


def test_ipv6_only_needs_disconnected():
    with pytest.raises(ValidationError, match="needs \"disconnected\": true"):
        OpenShiftOptions(ip_family="ipv6")
    opts = OpenShiftOptions(ip_family="ipv6", disconnected=True)
    assert opts.dual_stack is False


@pytest.mark.parametrize("extra, message", [
    ({"dual_stack": True}, "contradicts"),
    ({"platform": "baremetal", "topology": "compact"}, "platform"),
    ({"metallb": {"enabled": True}}, "MetalLB"),
])
def test_ipv6_only_refusals(extra, message):
    with pytest.raises(ValidationError, match=message):
        OpenShiftOptions(ip_family="ipv6", disconnected=True, **extra)


# install-config / agent-config

def test_install_config_ipv6_only():
    config = openshift_service.install_config("c", "g.lab", "10.62.0.0/24", 1, 0, "{}", "ssh-ed25519 x",
                                              machine_cidr6=PREFIX, ipv6_only=True)
    net = config["networking"]
    assert net["machineNetwork"] == [{"cidr": PREFIX}]
    assert net["clusterNetwork"] == [{"cidr": "fd01::/48", "hostPrefix": 64}]
    assert net["serviceNetwork"] == ["fd02::/112"]
    assert config["platform"] == {"none": {}}


def test_install_config_dual_stack_unchanged():
    net = openshift_service.install_config("c", "g.lab", "10.62.0.0/24", 1, 0, "{}", "k",
                                           machine_cidr6=PREFIX)["networking"]
    assert [m["cidr"] for m in net["machineNetwork"]] == ["10.62.0.0/24", PREFIX]


def test_agent_config_ipv6_only():
    config = openshift_service.agent_config(
        "c", "fd00:1:2:3::10", "fd00:1:2:3::1",
        [{"name": "c-master-0", "role": "master", "mac": "52:54:00:00:00:10"}])
    assert config["rendezvousIP"] == "fd00:1:2:3::10"
    assert config["additionalNTPSources"] == ["fd00:1:2:3::1"]
    assert "networkConfig" not in config["hosts"][0]  # DHCPv6 reservation (no nmstatectl needed on the host)


# router render

def test_dnsmasq_ipv6_only_reservation():
    conf = ELRouterBackend().dnsmasq_conf(group())
    assert "dhcp-host=52:54:00:00:00:10,[fd00:1:2:3::10],c-master-0" in conf
    assert "dhcp-mac=set:vmm-v6only,52:54:00:00:00:10" in conf
    assert "dhcp-ignore=tag:vmm-v6only,tag:!dhcpv6" in conf  # DHCPv4 only
    assert "host-record=c-master-0.g.lab,c-master-0,fd00:1:2:3::10\n" in conf
    assert "10.62.0.10," not in conf and "10.62.0.10\n" not in conf  # no IPv4 lease, no A record
    # a normal reservation keeps both families
    assert "dhcp-host=52:54:00:00:00:11,10.62.0.11,[fd00:1:2:3::11],other" in conf
    assert "dhcp-ignore" not in ELRouterBackend().dnsmasq_conf(group(reservations=[]))


def test_lb_backend_ipv6():
    lb = LoadBalancerSpec(name="x", port=443, backends=["[FD00:1:2:3:0::10]:443", "10.62.0.10:443"])
    assert lb.backends == ["[fd00:1:2:3::10]:443", "10.62.0.10:443"]
    with pytest.raises(ValidationError):
        LoadBalancerSpec(name="x", port=443, backends=["fd00::10:443"])  # IPv6 without brackets
    conf = ELRouterBackend().haproxy_conf(group())
    assert "    server c-api-0 fd00:1:2:3::10:6443 check" in conf  # haproxy: last colon = port


def test_registry_ipv6_front():
    spec = group()
    conf = ELRouterBackend().haproxy_conf(spec)
    assert f"bind :::8443 v6only ssl crt {registry_router.HAPROXY_PEM}" in conf
    assert "'\\1https://registry.g.lab:8443\\2'" in conf  # token realm -> registry.<domain>
    assert "server quay 10.62.0.1:8443 ssl verify none" in conf
    assert "VMM_REG_IPV6=1" in registry_router.env_file(spec)
    assert "haproxy_pem" in registry_router.script()
    # haproxy stays enabled for the registry front even without load balancers
    assert "systemctl disable --now haproxy" not in ELRouterBackend().apply_command(group(load_balancers=[]))
    # no IPv6: no front
    v4 = group(network={}, reservations=[], load_balancers=[])
    assert "vmm-registry6" not in ELRouterBackend().haproxy_conf(v4)
    assert "VMM_REG_IPV6=0" in registry_router.env_file(v4)


def test_ntp_serves_ipv6():
    cmd = ELRouterBackend.ntp_command(group())
    assert "allow 10.62.0.0/24" in cmd and f"allow {PREFIX}" in cmd
    assert "allow fd" not in ELRouterBackend.ntp_command(group(network={}, reservations=[], load_balancers=[]))
