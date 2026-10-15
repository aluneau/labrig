"""Emulated BMC port assignment (bmc_service.assign) and group export"""
import pytest

from app.models import Group
from app.schemas.group import GroupSpec


def _spec(name, cidr, port=None):
    spec = {"name": name, "cidr": cidr, "members": [{"name": "m1", "source": "empty", "bmc": True}]}
    if port:
        spec["bmc"] = {"port": port, "listen": "192.168.122.1"}
    return spec


def _add(db, name, cidr, port):
    db.add(Group(name=name, cidr=cidr, domain=f"{name}.lab", spec=_spec(name, cidr, port), status="ready"))
    db.commit()
    return db.query(Group).filter(Group.name == name).one()


def test_export_drops_assigned_bmc(db):
    """bmc.port / listen are assigned on this host: an export (also the base of "Save as template") must not
    carry them, or the re-imported group reuses the original group's emulator port"""
    from app.services.group_service import group_service
    group = _add(db, "e2e-bug-a", "10.77.1.0/24", 8180)
    spec = group_service.export_yaml(group)["spec"]
    assert "bmc" not in spec


def test_assign_refuses_a_port_another_group_uses(db):
    from app.services import bmc_service
    _add(db, "e2e-bug-a", "10.77.1.0/24", 8180)
    new = GroupSpec.model_validate(_spec("e2e-bug-b", "10.77.2.0/24", 8180))
    try:
        bmc_service.assign(db, new, None, None)
    except ValueError:
        return  # refused in plain words: fine
    assert new.bmc.port != 8180
