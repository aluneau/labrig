"""Regression coverage for physical ACM spoke teardown.

A BMH can disappear from the Kubernetes API only after BMO has removed the
corresponding Ironic node.  These tests keep the database reservation on the
safe side of that boundary.
"""
from types import SimpleNamespace

import pytest

from app.models import Cluster
from app.models.bmh import BareMetalHost
from app.services import acm_spokes
from app.services.acm_spokes import SpokeService


class PendingBmhRunner:
    def __init__(self):
        self.calls = []

    def oc(self, args, timeout):
        self.calls.append(args)
        if args[:2] == ["get", "bmh"]:
            return {"exitcode": 0, "stdout": "baremetalhost.metal3.io/dell-02\n", "stderr": ""}
        return {"exitcode": 0, "stdout": "", "stderr": ""}

    def log(self, _message):
        pass

    def sleep(self, _seconds):
        pass


def test_wait_for_bmh_deletion_times_out_while_bmh_exists(monkeypatch):
    runner = PendingBmhRunner()
    # The first monotonic call sets the deadline and the second verifies it has elapsed.
    ticks = iter((0, acm_spokes.BMH_DELETE_TIMEOUT + 1))
    monkeypatch.setattr(acm_spokes.time, "monotonic", lambda: next(ticks))

    with pytest.raises(TimeoutError, match="remain reserved"):
        SpokeService()._wait_for_bmh_deletion(runner, "spoke", ["dell-02"])


def test_release_keeps_real_host_reserved_when_bmh_cleanup_is_pending(db, monkeypatch):
    host = BareMetalHost(name="dell-02", bmc_address="https://bmc.example", bmc_username="root",
                         boot_mac="B8:59:9F:B1:16:AD", status="provisioning", cluster_name="spoke")
    hub = Cluster(name="hub", type="openshift", version="4.20", network="hub-net", domain="lab", spec={})
    spoke = Cluster(name="spoke", type="openshift", version="4.20", network="bare-metal", domain="lab", spec={
        "openshift": {"provisioner": {"kind": "acm", "hub_id": 1, "host_id": 1}},
    })
    db.add_all((host, hub, spoke))
    db.commit()
    spoke.spec["openshift"]["provisioner"]["hub_id"] = hub.id
    spoke.spec["openshift"]["provisioner"]["host_id"] = host.id
    db.commit()

    runner = PendingBmhRunner()
    service = SpokeService()
    monkeypatch.setattr(SpokeService, "hub_of", lambda _self, _db, _cluster: hub)
    monkeypatch.setattr(SpokeService, "svc", property(lambda _self: SimpleNamespace(
        to_dict=lambda _db, _cluster: {"status": "ready"},
    )))
    monkeypatch.setattr(SpokeService, "acm", property(lambda _self: SimpleNamespace(
        _runner=lambda _hub: runner, reap_lost=lambda *_args: None,
    )))
    from app.services import gitops_service
    monkeypatch.setattr(gitops_service.gitops_service, "remove_spoke", lambda *_args: False)
    monkeypatch.setattr(acm_spokes, "BMH_DELETE_TIMEOUT", 0)

    with pytest.raises(RuntimeError, match="Hub cleanup pending"):
        service.release(db, spoke)

    db.refresh(host)
    assert host.cluster_name == "spoke"
    assert host.status == "provisioning"
    assert "Hub cleanup pending" in host.status_message
    assert not any(call[:2] == ["delete", "namespace"] for call in runner.calls)
