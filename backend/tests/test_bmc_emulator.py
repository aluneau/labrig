"""The group BMC emulator only exposes the group's BMC members (libvirt test driver: one domain, "test")

Seen on the real service: with a group's BMC credentials, /Systems/<other VM>/VirtualMedia (InsertMedia redefines
the domain) and /Managers/<other VM uuid> answered 200 for VMs outside the group.
"""
import json
import socket

import pytest

pytest.importorskip("sushy_tools.emulator.main")
libvirt = pytest.importorskip("libvirt")


@pytest.fixture(scope="module")
def emulator(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("bmc")
    allow = tmp_path / "allow.json"
    allow.write_text("[]")
    state = tmp_path / "state"
    state.mkdir()
    real = socket.getaddrinfo
    from app.services import bmc_emulator
    app, _ = bmc_emulator.build_app({"listen": "127.0.0.1", "port": 1, "libvirt_uri": "test:///default",
                                     "auth_file": "", "allow_file": str(allow), "state_dir": str(state)})
    socket.getaddrinfo = real
    app.config["SUSHY_EMULATOR_AUTH_FILE"] = None
    uuid = libvirt.openReadOnly("test:///default").lookupByName("test").UUIDString()
    return app.test_client(), allow, uuid


@pytest.fixture()
def client(emulator):
    emulator[1].write_text("[]")
    return emulator


def test_other_vm_virtual_media_denied(client):
    c, _, uuid = client
    assert c.get(f"/redfish/v1/Systems/{uuid}").status_code == 404
    assert c.get(f"/redfish/v1/Systems/{uuid}/VirtualMedia").status_code == 404
    assert c.get(f"/redfish/v1/Systems/{uuid}/VirtualMedia/Cd").status_code == 404
    r = c.post(f"/redfish/v1/Systems/{uuid}/VirtualMedia/Cd/Actions/VirtualMedia.InsertMedia",
               json={"Image": "http://127.0.0.1:1/x.iso"})
    assert r.status_code == 404


def test_other_vm_manager_hidden(client):
    c, _, uuid = client
    assert c.get(f"/redfish/v1/Managers/{uuid}").status_code == 404
    members = json.loads(c.get("/redfish/v1/Managers").get_data(as_text=True))["Members"]
    assert members == []


def test_member_reachable(client):
    c, allow, uuid = client
    allow.write_text(json.dumps(["test"]))
    assert c.get(f"/redfish/v1/Systems/{uuid}").status_code == 200
    assert c.get(f"/redfish/v1/Systems/{uuid}/VirtualMedia").status_code == 200
    members = json.loads(c.get("/redfish/v1/Managers").get_data(as_text=True))["Members"]
    assert [m["@odata.id"] for m in members] == [f"/redfish/v1/Managers/{uuid}"]
