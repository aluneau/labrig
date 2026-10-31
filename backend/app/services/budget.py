"""Host resource budget: does what a request is about to create fit on this host? (docs/budget.md)

The host is often a desktop used for other things too: a lab that doesn't fit makes it swap or OOM-kill
something. Every create path (VM, lab group, group members, k3s / kubeadm cluster, cluster workers) builds a
list of items ``{name, memory_mib, vcpus, disk_gib, copy_gib, pool}`` with the ``plan_*`` helpers below and
calls ``enforce()``; ``POST /hosts/budget`` runs the same planners as a dry run for the UI.

Rules (kept simple on purpose):

- **Memory** (blocking): usable = ``MemAvailable`` (/proc/meminfo, what the kernel can hand out without
  swapping) - ``BUDGET_MEMORY_RESERVE_MIB`` (kept for the host) - what running VMs may still grow into
  (their max memory minus what they hold now, ``memoryStats`` rss: a fresh 16 GiB VM uses little until its
  guest touches its RAM) - the max memory of shut-off VMs the same operation starts (``starting``, e.g. the
  stopped router of the group a kubeadm cluster joins). The new items' memory must fit in that.
  New VMs count even when created stopped: they are made to be run.
- **vCPU** (warning only): vCPUs of running + starting + new VMs vs host CPUs above ``BUDGET_VCPU_OVERCOMMIT``.
- **Disk** per storage pool: ``copy_gib`` = bytes written at creation (cloud image copies are full
  copies of the image) must fit in the pool's free space (blocking); the full virtual size of the new
  disks (qcow2, thin: they grow as the guest writes) above the free space is a warning.

``BUDGET_ENFORCE=false`` turns blocking problems into warnings. Blocking = ``BudgetExceeded`` -> HTTP 409
with the plain-words message; ``force=true`` on the request overrides (writes are admin-only anyway).

``enforce()`` only acts inside ``request()`` (set by the API endpoints), once per request: the first
check of a request covers everything it creates (a kubeadm cluster's check includes its auto-created group's
router, so the nested group create doesn't check again). Background tasks (group create task, template
cluster task) never check: their request did.
"""
import contextvars
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

GIB = 1024 ** 3
MEMINFO = "/proc/meminfo"
FORCE_HELP = "Create even when the host resource budget says it doesn't fit (409 otherwise; docs/budget.md)"

_request: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = contextvars.ContextVar("budget_request", default=None)


class BudgetExceeded(Exception):
    """The request doesn't fit on the host (-> 409). result = check()'s result"""

    def __init__(self, result: Dict[str, Any]):
        self.result = result
        super().__init__(result["message"])


@contextmanager
def request(force: bool = False) -> Iterator[None]:
    """Scope of one API request: enforce() checks once inside it, force skips the refusal"""
    token = _request.set({"force": bool(force), "done": False})
    try:
        yield
    finally:
        _request.reset(token)


def skip() -> None:
    """The caller checked the request itself (OpenShift's stricter check): no generic check in it"""
    state = _request.get()
    if state is not None:
        state["done"] = True


def item(name: str, memory_mib: int, vcpus: int, disk_gib: float = 0, copy_gib: float = 0,
         pool: Optional[str] = None) -> Dict[str, Any]:
    return {"name": name, "memory_mib": int(memory_mib), "vcpus": int(vcpus), "disk_gib": float(disk_gib or 0),
            "copy_gib": float(copy_gib or 0), "pool": pool or settings.DEFAULT_POOL_NAME}


# ------------------------------------------------------------------ host readings (mocked in tests)

def meminfo() -> Dict[str, int]:
    """MemTotal / MemAvailable in MiB (empty when /proc/meminfo can't be read)"""
    out: Dict[str, int] = {}
    try:
        with open(MEMINFO) as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    out[key] = int(rest.split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        return {}
    return out


def host_cpus() -> int:
    import os
    return os.cpu_count() or 1


def domains() -> List[Dict[str, Any]]:
    from app.libvirt_client import libvirt_client
    return libvirt_client.budget_domains()


def pool_free_gib(pool: str) -> Optional[float]:
    """Free space of a pool (GiB); a pool that doesn't exist yet (created on first use) = its path's filesystem"""
    from app.libvirt_client import libvirt_client
    free = libvirt_client.pool_available(pool)
    if free is None and pool == settings.DEFAULT_POOL_NAME:
        import os
        import shutil
        path = settings.DEFAULT_POOL_PATH
        while path and not os.path.exists(path):
            path = os.path.dirname(path)
        try:
            free = shutil.disk_usage(path or "/").free
        except OSError:
            return None
    return None if free is None else free / GIB


# ------------------------------------------------------------------ the check

def _gib(mib: float) -> str:
    gib = mib / 1024
    return f"{gib:.0f} GiB" if gib >= 10 or abs(gib - round(gib)) < 0.05 else f"{gib:.1f} GiB"


def check(items: List[Dict[str, Any]], starting: Iterable[str] = ()) -> Dict[str, Any]:
    """Compare what will be created with the host (see the module docstring for the rules)"""
    starting = set(starting)
    reserve = settings.BUDGET_MEMORY_RESERVE_MIB
    mem = meminfo()
    doms = domains()
    by_name = {d["name"]: d for d in doms}
    running = [d for d in doms if d["state"] in ("running", "paused", "blocked")]
    growth = sum(max(0, d["max_memory"] - (d["rss"] if d.get("rss") is not None else d["max_memory"])) // 1024
                 for d in running)
    to_start = [by_name[n] for n in sorted(starting) if n in by_name and by_name[n]["state"] in ("shutoff", "shutdown", "crashed")]
    starting_mib = sum(d["max_memory"] // 1024 for d in to_start)
    need_mem = sum(i["memory_mib"] for i in items)
    need_cpu = sum(i["vcpus"] for i in items)
    available = mem.get("MemAvailable")
    usable = None if available is None else available - reserve - growth - starting_mib

    problems: List[str] = []
    warnings: List[str] = []
    if usable is not None and need_mem > max(usable, 0):
        extra = []
        if growth:
            extra.append(f"running VMs may still take {_gib(growth)}")
        if starting_mib:
            extra.append(f"{', '.join(d['name'] for d in to_start)} start{'s' if len(to_start) == 1 else ''} too "
                         f"({_gib(starting_mib)})")
        extra.append(f"{_gib(reserve)} kept for the host")
        problems.append(f"Memory: needs {_gib(need_mem)}, {_gib(max(usable, 0))} available "
                        f"({_gib(available)} free, {'; '.join(extra)}): stop something or shrink the VMs")
    elif usable is not None and need_mem > 0.8 * usable:
        warnings.append(f"Memory: needs {_gib(need_mem)} of the {_gib(usable)} available: little left for the host")

    cpus = host_cpus()
    allocated = sum(d["vcpu"] for d in running) + sum(d["vcpu"] for d in to_start)
    total_vcpus = allocated + need_cpu
    ratio = total_vcpus / cpus
    if need_cpu and ratio > settings.BUDGET_VCPU_OVERCOMMIT:
        warnings.append(f"vCPU: {total_vcpus} vCPUs on {cpus} host CPUs ({ratio:.1f}x, above "
                        f"{settings.BUDGET_VCPU_OVERCOMMIT:g}x): the VMs will be slow under load")
    big = [i for i in items if i["vcpus"] > cpus]
    if big:
        warnings.append(f"vCPU: {big[0]['name']} asks {big[0]['vcpus']} vCPUs, the host has {cpus}")

    pools: List[Dict[str, Any]] = []
    for pool in sorted({i["pool"] for i in items}):
        disk = sum(i["disk_gib"] for i in items if i["pool"] == pool)
        copy = sum(i["copy_gib"] for i in items if i["pool"] == pool)
        if not disk and not copy:
            continue
        free = pool_free_gib(pool)
        pools.append({"name": pool, "free_gib": None if free is None else round(free, 1),
                      "disk_gib": round(disk, 1), "copy_gib": round(copy, 1)})
        if free is None:
            continue
        if copy > free:
            problems.append(f"Disk: pool {pool} needs {copy:.1f} GiB now (cloud image copies), {free:.1f} GiB free: "
                            "free some space or use smaller disks")
        elif disk > free:
            warnings.append(f"Disk: the new disks can grow to {disk:.0f} GiB, pool {pool} has {free:.0f} GiB free "
                            "(thin disks: fine until the guests fill them)")

    force = bool((_request.get() or {}).get("force"))
    blocking = bool(problems) and settings.BUDGET_ENFORCE
    message = ""
    if problems:
        message = "Not enough host resources: " + "; ".join(problems)
        message += (". Pass force=true to create anyway." if blocking and not force
                    else " (created anyway)" if blocking else " (BUDGET_ENFORCE is off: warning only)")
    return {
        "ok": not problems, "blocking": blocking, "forced": force and blocking, "enforced": settings.BUDGET_ENFORCE,
        "problems": problems, "warnings": warnings, "message": message,
        "need": {"memory_mib": need_mem, "vcpus": need_cpu,
                 "disk_gib": round(sum(i["disk_gib"] for i in items), 1),
                 "copy_gib": round(sum(i["copy_gib"] for i in items), 1)},
        "host": {"memory_total_mib": mem.get("MemTotal"), "memory_available_mib": available,
                 "reserve_mib": reserve, "running_growth_mib": growth, "starting_mib": starting_mib,
                 "starting": [d["name"] for d in to_start], "usable_mib": usable,
                 "cpus": cpus, "vcpus_allocated": allocated, "vcpu_overcommit": settings.BUDGET_VCPU_OVERCOMMIT,
                 "pools": pools},
        "items": items,
    }


def enforce(items: List[Dict[str, Any]], starting: Iterable[str] = ()) -> Optional[Dict[str, Any]]:
    """Check once per API request (no-op outside one, or when it was checked already); raise BudgetExceeded
    when it doesn't fit, unless forced or BUDGET_ENFORCE is off"""
    state = _request.get()
    if state is None or state["done"] or not items:
        return None
    state["done"] = True
    try:
        result = check(items, starting)
    except BudgetExceeded:
        raise
    except Exception as e:  # never block a create on a failed reading (e.g. libvirt stopped: it fails itself)
        logger.warning(f"Host budget check skipped: {e}")
        return None
    if result["blocking"] and not state["force"]:
        raise BudgetExceeded(result)
    if result["problems"] or result["warnings"]:
        logger.warning("Host budget: " + "; ".join(result["problems"] + result["warnings"]))
    return result


# ------------------------------------------------------------------ planners (what a request creates)

def _image_gib(db, image_id: Optional[int] = None, ref: Optional[str] = None) -> float:
    """Size of a cloud image file (its copy is written in full at creation); 0 when unknown"""
    try:
        if image_id:
            from app.services.cloud_image_service import cloud_image_service
            image = cloud_image_service.get_image(db, image_id)
        else:
            from app.services.group_service import group_service
            image = group_service.resolve_image(db, ref)
        return (image.size or 0) / GIB if image is not None else 0.0
    except Exception:
        return 0.0


def plan_vm(db, data, image=None) -> List[Dict[str, Any]]:
    """VMCreate: its disk (a cloud image copy is grown to disk_size: real = the image size)"""
    copy = ((image.size or 0) / GIB if image is not None else _image_gib(db, data.cloud_image_id)) \
        if data.cloud_image_id else 0.0
    disk = max(float(data.disk_size or 0), copy)
    return [item(data.name, data.memory, data.vcpu, disk, copy)]


def plan_members(db, group_name: str, members) -> List[Dict[str, Any]]:
    from app.services.group_service import member_vm_name
    out = []
    for m in members:
        copy = _image_gib(db, ref=m.image) if m.source == "cloud_image" else 0.0
        out.append(item(member_vm_name(group_name, m.name), m.memory, m.vcpu, max(float(m.disk_size), copy), copy))
    return out


def plan_router(db, group_name: str, router) -> Dict[str, Any]:
    from app.services.group_service import group_service, router_vm_name
    ref = router.image
    if not ref:
        try:
            ref = group_service._default_router_image(db)
        except Exception:
            ref = None
    copy = _image_gib(db, ref=ref) if ref else 0.0
    disk = max(float(router.disk_size), copy) + (router.registry.disk_gb if router.registry.enabled else 0)
    return item(router_vm_name(group_name), router.effective_memory(), router.effective_vcpu(), disk, copy)


def plan_group(db, spec) -> List[Dict[str, Any]]:
    """GroupSpec: router + members"""
    return [plan_router(db, spec.name, spec.router)] + plan_members(db, spec.name, spec.members)


def plan_cluster(db, data) -> Dict[str, Any]:
    """ClusterCreate (k3s / kubeadm; OpenShift has its own stricter check): nodes, + the router of an
    auto-created group (kubeadm), or the stopped router of the group it joins (starting).
    Returns {items, starting}."""
    from app.schemas.group import GroupSpec, RouterSpec
    from app.services.group_service import group_service, router_vm_name
    if data.type == "openshift":
        return {"items": [], "starting": []}
    image_id = data.cloud_image_id
    if not image_id:
        try:
            from app.services.cluster_service import cluster_service
            image_id = cluster_service._pick_image(db, None).id
        except Exception:
            image_id = None
    copy = _image_gib(db, image_id) if image_id else 0.0
    items = []
    for role, count, res in (("cp", data.ctlplanes, data.ctlplane), ("worker", data.workers, data.worker)):
        for i in range(count):
            items.append(item(f"{data.name}-{role}-{i}", res.memory, res.vcpu, max(float(res.disk_size), copy), copy))
    starting: List[str] = []
    if data.type == "kubeadm":
        if data.group_id is None:
            router = RouterSpec(memory=data.router_memory) if getattr(data, "router_memory", None) else RouterSpec()
            if data.disconnected:
                from app.services.k8s_mirror import REGISTRY_SIZES
                router.registry.enabled = True
                router.registry.memory_mb = REGISTRY_SIZES["memory_mb"]
                router.registry.vcpus = REGISTRY_SIZES["vcpus"]
                router.registry.disk_gb = REGISTRY_SIZES["disk_gb"]
            items.insert(0, plan_router(db, data.name, router))
        else:
            group = group_service.get_group(db, data.group_id)
            if group is not None:
                starting.append(router_vm_name(group.name))
                gspec = GroupSpec.model_validate(group.spec)
                if data.disconnected and not gspec.router.registry.enabled:
                    from app.services.k8s_mirror import REGISTRY_SIZES
                    extra = max(0, REGISTRY_SIZES["memory_mb"] - gspec.router.memory)
                    if extra:
                        items.append(item(f"{router_vm_name(group.name)} (mirror registry)", extra, 0,
                                          REGISTRY_SIZES["disk_gb"]))
    return {"items": items, "starting": starting}


def dry_run(db, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /hosts/budget: the same planners as the creates, on a partial form (names may be missing while
    the user types). One of: vm (VMCreate), group (GroupSpec: router + members), group_id + members (add
    members), cluster (ClusterCreate), cluster_id + count (add workers), items (explicit); + starting."""
    from app.schemas import ClusterCreate, VMCreate
    from app.schemas.group import MemberSpec, RouterSpec
    from app.services.group_service import group_service

    def members(raw: List[Dict[str, Any]]) -> list:
        return [MemberSpec.model_validate({**m, "name": m.get("name") or f"m{i}"}) for i, m in enumerate(raw or [])]

    starting = list(body.get("starting") or [])
    note = None
    if body.get("vm") is not None:
        vm = VMCreate.model_validate({**body["vm"], "name": body["vm"].get("name") or "new-vm"})
        items = plan_vm(db, vm)
    elif body.get("group") is not None:
        g = body["group"]
        name = g.get("name") or "group"
        items = [plan_router(db, name, RouterSpec.model_validate(g.get("router") or {}))] \
            + plan_members(db, name, members(g.get("members")))
    elif body.get("group_id") is not None:
        group = group_service.get_group(db, int(body["group_id"]))
        if group is None:
            raise LookupError("Lab group not found")
        items = plan_members(db, group.name, members(body.get("members")))
    elif body.get("cluster") is not None:
        data = ClusterCreate.model_validate({**body["cluster"], "name": body["cluster"].get("name") or "cluster"})
        plan = plan_cluster(db, data)
        items, starting = plan["items"], starting + plan["starting"]
        if data.type == "openshift":
            note = "OpenShift checks its own RAM rule at create (no overcommit: etcd)"
    elif body.get("cluster_id") is not None:
        from app.services.cluster_service import cluster_service
        cluster = cluster_service.get_cluster(db, int(body["cluster_id"]))
        if cluster is None:
            raise LookupError("Cluster not found")
        items = [] if cluster.type == "openshift" else plan_workers(db, cluster, int(body.get("count") or 1),
                                                                     body.get("worker"))
        if cluster.type == "openshift":
            note = "OpenShift checks its own RAM rule when adding workers"
    else:
        items = [item(i.get("name") or f"item{n}", i.get("memory_mib", 0), i.get("vcpus", 0), i.get("disk_gib", 0),
                      i.get("copy_gib", 0), i.get("pool")) for n, i in enumerate(body.get("items") or [])]
    result = check(items, starting)
    result["note"] = note
    return result


def plan_workers(db, cluster, count: int, worker: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """New workers of a k3s / kubeadm cluster: the cluster's worker size"""
    spec = cluster.spec or {}
    res = worker or spec.get("worker") or {}
    copy = _image_gib(db, spec.get("cloud_image_id")) if spec.get("cloud_image_id") else 0.0
    return [item(f"{cluster.name}-worker-new{i}", res.get("memory", 2048), res.get("vcpu", 2),
                 max(float(res.get("disk_size", 20)), copy), copy) for i in range(count)]
