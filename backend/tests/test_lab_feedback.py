"""Fixes from a lab run report: power history, router NTP without upstream, guest agent on OpenShift nodes,
ClusterVersion naming guard, automatic group subnet"""
from app.services import power_history


def test_power_history_tags_task_and_merges(tmp_path):
    power_history.set_actor("task 7: Start cluster x")
    power_history.app_action("ph-vm", "reboot (ACPI)")
    power_history.set_actor(None)
    power_history.record("ph-vm", "rebooted", "libvirt", "reset")
    bmc = tmp_path / "bmc-actions.jsonl"
    bmc.write_text('{"time": "2000-01-01T00:00:00+00:00", "identity": "uuid-1", "action": "Redfish reset On", '
                   '"client": "10.0.0.5"}\n{"identity": "other"}\nnot json\n')
    items = power_history.history("ph-vm", "uuid-1", bmc)
    assert items[0]["event"] == "Redfish reset On"  # oldest first
    assert items[1]["source"] == "vm-manager" and items[1]["detail"] == "task 7: Start cluster x"
    summary = power_history.summary(items)
    assert summary["by_source"] == {"BMC": 1, "vm-manager": 1, "guest": 1}


def test_router_serves_ntp_without_upstream():
    from app.schemas.group import GroupSpec
    from app.services.router_service import ELRouterBackend
    cmd = ELRouterBackend.ntp_command(GroupSpec(name="g", cidr="10.42.9.0/24"))
    assert "local stratum 8" in cmd and "allow 10.42.9.0/24" in cmd


def test_qemu_ga_machineconfigs():
    from urllib.parse import unquote
    from app.services.openshift_addons import AddonRunner
    mcs = AddonRunner.qemu_ga_machineconfigs(["master", "worker"])
    assert [m["metadata"]["labels"]["machineconfiguration.openshift.io/role"] for m in mcs] == ["master", "worker"]
    files = {f["path"]: unquote(f["contents"]["source"][len("data:,"):]) for f in mcs[0]["spec"]["config"]["storage"]["files"]}
    assert 'FILTER_RPC_ARGS=""' in files["/etc/sysconfig/qemu-ga"]
    assert all(f["overwrite"] for f in mcs[0]["spec"]["config"]["storage"]["files"])  # RHCOS ships the file
    assert files["/etc/vm-manager/qemu-ga-permissive.cil"].strip() == "(typepermissive virt_qemu_ga_t)"
    unit = mcs[0]["spec"]["config"]["systemd"]["units"][0]["contents"]
    assert "--no-block" in unit  # try-restart from inside a oneshot must not wait on the agent's job


def test_misnamed_clusterversion():
    from app.mcp.tools import _misnamed_clusterversion as bad
    assert bad("kind: ClusterVersion\nmetadata:\n  name: cluster\n")
    assert not bad("kind: ClusterVersion\nmetadata:\n  name: version\nspec:\n  overrides:\n  - kind: Deployment\n    name: x\n")
    assert not bad("kind: ConfigMap\nmetadata:\n  name: ClusterVersion\n")


def test_group_cidr_auto(monkeypatch):
    from app.schemas.group import GroupSpec, GroupSpecCreate
    from app.services import template_service as ts
    monkeypatch.setattr(ts.template_service, "free_cidr", lambda db: "10.42.3.0/24")
    assert GroupSpecCreate(name="g").cidr == "10.42.3.0/24"
    assert GroupSpecCreate(name="g", cidr="auto").cidr == "10.42.3.0/24"
    assert GroupSpecCreate(name="g", cidr="10.42.8.0/24").cidr == "10.42.8.0/24"
    import pytest
    with pytest.raises(Exception):
        GroupSpec(name="g")  # updates still need it
