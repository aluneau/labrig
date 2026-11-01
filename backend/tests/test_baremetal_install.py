"""Bare-metal Linux installs: kickstart (sudo for the key-only admin, done callback), one-line inputs, and the
installer's done callback (public token URL: install installed, host available, ISO gone, URL dead)"""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.database import SessionLocal
from app.main import app
from app.models.bmh import BareMetalHost, BareMetalInstall
from app.schemas.bmh import BareMetalLinuxInstall
from app.services import baremetal_install_service as bis

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBk test@host"


def _req(**kw):
    return BareMetalLinuxInstall(**{"name": "el9", "iso_name": "alma.iso", "ssh_authorized_key": KEY, **kw})


def test_kickstart():
    ks = bis.BareMetalInstallService._ks(_req(admin_username="ops", repo_url="https://mirror/x/BaseOS"),
                                          "http://10.0.0.1:8000/bmh-install/T/done")
    lines = ks.splitlines()
    assert "url --url=https://mirror/x/BaseOS" in lines and "rootpw --lock" in lines
    assert f'sshkey --username=ops "{KEY}"' in lines
    assert "echo 'ops ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/vmm-ops" in lines
    assert lines[lines.index("%post --nochroot") + 1] == "curl -fsS -m 30 -X POST http://10.0.0.1:8000/bmh-install/T/done || true"
    assert "cdrom" in bis.BareMetalInstallService._ks(_req(), "u").splitlines()


@pytest.mark.parametrize("bad", [{"ssh_authorized_key": KEY + "\n%post\nrm -rf /\n%end"},
                                 {"ssh_authorized_key": KEY + '" --extra'},
                                 {"repo_url": "https://x/y\nrootpw x"}, {"repo_url": "file:///etc"}])
def test_one_line_inputs(bad):
    with pytest.raises(ValidationError):
        _req(**bad)


def test_done_callback(monkeypatch, tmp_path):
    ejected = []
    monkeypatch.setattr(bis.BareMetalInstallService, "_eject", staticmethod(lambda db, task, hid: ejected.append(hid) or {}))
    work = tmp_path / "tok"
    work.mkdir()
    (work / "installer.iso").write_bytes(b"iso")
    with SessionLocal() as db:
        host = BareMetalHost(name="bm1", bmc_address="https://10.0.0.9", bmc_username="root", status="provisioning")
        db.add(host)
        db.commit()
        inst = BareMetalInstall(host_id=host.id, name="el9", installer_iso="/x.iso", target_disk="/dev/sda",
                                token="tok-123", workdir=str(work), status="booting")
        db.add(inst)
        db.commit()
        hid, iid = host.id, inst.id
    client = TestClient(app)
    assert client.get("/bmh-install/tok-123/installer.iso").status_code == 200
    assert client.post("/bmh-install/wrong/done").status_code == 404
    assert client.post("/bmh-install/tok-123/done").json() == {"status": "installed"}
    with SessionLocal() as db:
        assert db.get(BareMetalInstall, iid).status == "installed"
        assert db.get(BareMetalHost, hid).status == "available"
    assert not work.exists()
    assert client.get("/bmh-install/tok-123/installer.iso").status_code == 404
    assert client.post("/bmh-install/tok-123/done").status_code == 404  # once
    bis.task_service.wait(max(bis.task_service._running, default=0), 5)
    assert ejected == [hid]
