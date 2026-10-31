"""Host resource budget (services/budget.py): rules, enforce() scoping, 409 + force through the API"""
import pytest

from app.config import settings
from app.services import budget

GIB = 1024 ** 3


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A 64 GiB / 8 CPU host with 20 GiB MemAvailable, a 100 GiB free pool, and a few VMs"""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       67108864 kB\nMemFree:         1000000 kB\nMemAvailable:   20971520 kB\n")
    monkeypatch.setattr(budget, "MEMINFO", str(meminfo))
    state = {
        "domains": [
            # running 8 GiB VM holding 2 GiB: may still grow by 6 GiB
            {"name": "busy", "state": "running", "max_memory": 8 * 1024 * 1024, "vcpu": 4, "rss": 2 * 1024 * 1024},
            # stopped 4 GiB router: counts only when the operation starts it
            {"name": "g-rtr", "state": "shutoff", "max_memory": 4 * 1024 * 1024, "vcpu": 2, "rss": None},
        ],
        "pools": {"default": 100.0},
    }
    monkeypatch.setattr(budget, "domains", lambda: state["domains"])
    monkeypatch.setattr(budget, "pool_free_gib", lambda pool: state["pools"].get(pool))
    monkeypatch.setattr(budget, "host_cpus", lambda: 8)
    monkeypatch.setattr(settings, "BUDGET_MEMORY_RESERVE_MIB", 2048)
    monkeypatch.setattr(settings, "BUDGET_VCPU_OVERCOMMIT", 4.0)
    monkeypatch.setattr(settings, "BUDGET_ENFORCE", True)
    return state


def test_meminfo_parsed(host):
    assert budget.meminfo() == {"MemTotal": 65536, "MemAvailable": 20480}


def test_fits(host):
    r = budget.check([budget.item("a", 4096, 2, 20, 1)])
    # usable = 20 GiB available - 2 GiB reserve - 6 GiB the running VM may still take
    assert r["host"]["usable_mib"] == 20480 - 2048 - 6144
    assert r["ok"] and not r["blocking"] and r["problems"] == [] and r["warnings"] == []


def test_memory_blocks_with_numbers(host):
    r = budget.check([budget.item("big", 16384, 2)])
    assert not r["ok"] and r["blocking"]
    assert "needs 16 GiB, 12 GiB available" in r["problems"][0]
    assert "stop something" in r["problems"][0]
    assert "force=true" in r["message"]


def test_starting_vms_count(host):
    item = [budget.item("a", 10 * 1024, 1)]
    assert budget.check(item)["ok"]
    r = budget.check(item, starting=["g-rtr", "busy", "unknown"])  # only the shut-off one adds up
    assert r["host"]["starting"] == ["g-rtr"] and r["host"]["starting_mib"] == 4096
    assert not r["ok"] and "g-rtr starts too" in r["problems"][0]


def test_vcpu_overcommit_is_a_warning(host):
    r = budget.check([budget.item("a", 512, 30)])  # 4 running + 30 = 34 on 8 CPUs
    assert r["ok"] and any("34 vCPUs on 8 host CPUs" in w for w in r["warnings"])


def test_disk_rules(host):
    r = budget.check([budget.item("a", 512, 1, disk_gib=150, copy_gib=3)])
    assert r["ok"] and any("can grow to 150 GiB" in w for w in r["warnings"])  # thin: warning
    r = budget.check([budget.item("a", 512, 1, disk_gib=150, copy_gib=120)])
    assert r["blocking"] and "pool default needs 120.0 GiB now" in r["problems"][0]  # real copies: blocking


def test_enforce_off_only_warns(host, monkeypatch):
    monkeypatch.setattr(settings, "BUDGET_ENFORCE", False)
    with budget.request():
        r = budget.enforce([budget.item("big", 64 * 1024, 1)])
    assert r["problems"] and not r["blocking"] and "warning only" in r["message"]


def test_enforce_scoping(host):
    big = [budget.item("big", 64 * 1024, 1)]
    assert budget.enforce(big) is None  # outside an API request (background tasks): never checks
    with budget.request():
        with pytest.raises(budget.BudgetExceeded) as e:
            budget.enforce(big)
        assert e.value.result["need"]["memory_mib"] == 64 * 1024
    with budget.request(force=True):
        r = budget.enforce(big)
        assert r["forced"] and "created anyway" in r["message"]
    with budget.request():
        budget.enforce([budget.item("small", 512, 1)])
        assert budget.enforce(big) is None  # once per request: the outer create's check covers the nested ones
    with budget.request():
        budget.skip()  # OpenShift's own check
        assert budget.enforce(big) is None


def test_api_vm_create_409_then_force(host, client, admin_headers):
    body = {"name": "bud-test", "memory": 64 * 1024, "vcpu": 1, "disk_size": 0}
    dry = client.post("/api/v1/hosts/budget", json={"vm": body}, headers=admin_headers)
    assert dry.status_code == 200 and dry.json()["blocking"]
    assert dry.json()["need"]["memory_mib"] == 64 * 1024
    r = client.post("/api/v1/vms", json=body, headers=admin_headers)
    assert r.status_code == 409
    assert "Memory: needs 64 GiB" in r.json()["detail"] and r.json()["budget"]["problems"]
    vms = client.get("/api/v1/vms", headers=admin_headers).json()
    assert not any(v["name"] == "bud-test" for v in vms)  # nothing created
    r = client.post("/api/v1/vms?force=true", json=body, headers=admin_headers)
    assert r.status_code == 201, r.text
    client.delete(f"/api/v1/vms/{r.json()['id']}", headers=admin_headers)


def test_dry_run_viewer_refused(host, client, viewer_headers):
    r = client.post("/api/v1/hosts/budget", json={"vm": {"memory": 1024}}, headers=viewer_headers)
    assert r.status_code == 403


def test_dry_run_group_and_cluster(host, client, admin_headers):
    r = client.post("/api/v1/hosts/budget", headers=admin_headers, json={
        "group": {"router": {"memory": 512}, "members": [{"memory": 2048, "vcpu": 2, "source": "empty"}]}})
    assert r.status_code == 200, r.text
    assert r.json()["need"]["memory_mib"] == 512 + 2048
    r = client.post("/api/v1/hosts/budget", headers=admin_headers, json={
        "cluster": {"type": "k3s", "ctlplanes": 1, "workers": 2, "worker": {"memory": 4096, "vcpu": 2, "disk_size": 20}}})
    assert r.status_code == 200, r.text
    assert r.json()["need"]["memory_mib"] == 2048 + 2 * 4096 and len(r.json()["items"]) == 3
