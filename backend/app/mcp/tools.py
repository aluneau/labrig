"""MCP tools. Most call the app's own REST API in-process with the caller's credentials (same validation,
role checks and audit log as the UI); admin=True tools act directly (guest agent, ssh, kubectl)."""
import asyncio
import json
import shlex
from typing import Any, Dict, List, Optional, Union

import httpx
import libvirt
from starlette.concurrency import run_in_threadpool

from app.mcp.registry import Literal, tool
from app.services.auth_service import CSRF_HEADER

Ref = Union[int, str]  # id or name
MAX_TEXT = 200_000


class ApiError(Exception):
    pass


class Ctx:
    def __init__(self, user: Any, headers: Dict[str, str]):
        self.user = user
        self.headers = headers

    async def api(self, method: str, path: str, body: Any = None, query: Optional[Dict[str, Any]] = None,
                  timeout: float = 300) -> Any:
        from app.main import app  # circular at import time
        headers = {CSRF_HEADER: "mcp"}
        for name in ("authorization", "cookie"):
            if name in self.headers:
                headers[name] = self.headers[name]
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/api/"):
            path = "/api/v1" + path
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 0))
        async with httpx.AsyncClient(transport=transport, base_url="http://vm-manager", timeout=timeout) as c:
            r = await c.request(method.upper(), path, json=body, params=_clean(query or {}), headers=headers)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ApiError(f"{method.upper()} {path}: HTTP {r.status_code}: {_text(detail)}")
        if not r.content:
            return {"status": r.status_code}
        if "json" in r.headers.get("content-type", ""):
            return r.json()
        return r.text

    @property
    def is_viewer(self) -> bool:
        return self.user is not None and getattr(self.user, "role", "admin") != "admin"

    def require_admin(self) -> None:
        if self.is_viewer:
            raise ApiError("This tool needs an admin account (viewers are read-only)")


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _text(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, default=str)


async def _find(ctx: Ctx, kind: str, ref: Ref) -> Dict[str, Any]:
    """Resolve an id or a name among /vms, /groups or /clusters"""
    if isinstance(ref, int) or str(ref).isdigit():
        return await ctx.api("GET", f"/{kind}/{int(ref)}")
    items = await ctx.api("GET", f"/{kind}")
    for item in items:
        if item.get("name") == ref:
            return item
    names = sorted(i.get("name", "?") for i in items)
    names = ", ".join(names[:40]) + (f" … ({len(names)} in all)" if len(names) > 40 else "") if names else "none"
    raise ApiError(f"No {kind[:-1]} named '{ref}' (existing: {names})")


def _brief_vm(v: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("id", "name", "status", "vcpu", "memory", "description")
    return {k: v[k] for k in keys if v.get(k) not in (None, [], "")}


# ---------------------------------------------------------------------------------------------- overview / API

@tool
async def overview(ctx: Ctx) -> Any:
    """Start here: host resources, libvirt state, VMs, lab groups, clusters and running tasks in one call."""
    async def safe(path: str) -> Any:
        try:
            return await ctx.api("GET", path)
        except ApiError as e:
            return {"error": str(e)}
    libvirt_state, resources, vms, groups, clusters, tasks = await asyncio.gather(
        safe("/hosts/libvirt"), safe("/hosts/resources"), safe("/vms"), safe("/groups"), safe("/clusters"),
        safe("/tasks"))
    return {
        "libvirt": libvirt_state,
        "host_resources": resources,
        "vms": [_brief_vm(v) for v in vms] if isinstance(vms, list) else vms,
        "groups": [{"id": g["id"], "name": g["name"], "status": g.get("status")} for g in groups]
        if isinstance(groups, list) else groups,
        "clusters": [{k: c.get(k) for k in ("id", "name", "type", "status", "api_endpoint", "console_url")}
                     for c in clusters] if isinstance(clusters, list) else clusters,
        "running_tasks": [t for t in tasks if t.get("status") in ("pending", "running")][:20]
        if isinstance(tasks, list) else tasks,
    }


@tool
async def api_describe(ctx: Ctx, path_contains: Optional[str] = None, method: Optional[str] = None) -> Any:
    """Describe the vm-manager REST API (OpenAPI). Without arguments: every endpoint (method, path, summary).
    With path_contains: the matching endpoints with their full request body JSON schema, including nested
    models (group spec, cluster spec with OpenShift options, template parameters...). Use it before
    api_request or create_group / create_cluster to build complex bodies exactly.

    Args:
        path_contains: substring of the path, e.g. '/groups/{group_id}/wireguard' or 'clusters'
        method: restrict to one HTTP method (GET, POST, PUT, PATCH, DELETE)
    """
    from app.main import app
    spec = app.openapi()
    components = spec.get("components", {}).get("schemas", {})
    out: List[Dict[str, Any]] = []
    for path, ops in spec.get("paths", {}).items():
        if not path.startswith("/api/v1"):
            continue
        for m, op in ops.items():
            if method and m.upper() != method.upper():
                continue
            if path_contains is None:
                out.append({"method": m.upper(), "path": path[len("/api/v1"):], "summary": op.get("summary")})
            elif path_contains in path:
                entry: Dict[str, Any] = {"method": m.upper(), "path": path[len("/api/v1"):],
                                         "summary": op.get("summary"), "description": op.get("description"),
                                         "parameters": [{k: p.get(k) for k in ("name", "in", "required", "schema")}
                                                        for p in op.get("parameters", [])]}
                body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
                if body:
                    entry["body_schema"] = body
                    entry["models"] = _models(body, components)
                out.append(entry)
    if path_contains is not None and not out:
        raise ApiError(f"No endpoint path contains '{path_contains}' (call api_describe without arguments)")
    return out


def _models(schema: Any, components: Dict[str, Any]) -> Dict[str, Any]:
    """Every component schema referenced (transitively) from schema"""
    found: Dict[str, Any] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in found and name in components:
                    found[name] = components[name]
                    walk(components[name])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(schema)
    return found


@tool
async def api_request(ctx: Ctx, method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"], path: str,
                      body: Optional[Dict[str, Any]] = None, query: Optional[Dict[str, Any]] = None) -> Any:
    """Call any vm-manager REST endpoint (everything the web UI can do). Paths are relative to /api/v1,
    e.g. GET /groups/3/leases, PUT /groups/3 with a full group spec, PUT /clusters/2/metallb.
    Use api_describe first to get the exact body schema.

    Args:
        method: HTTP method
        path: endpoint path relative to /api/v1, e.g. '/vms/12/disks'
        body: JSON request body
        query: query string parameters, e.g. {"delete_disks": true}
    """
    return await ctx.api(method, path, body, query)


# ---------------------------------------------------------------------------------------------- VMs

@tool
async def list_vms(ctx: Ctx) -> Any:
    """List virtual machines (id, name, state, vCPUs, memory MiB, description). IPs, disks, NICs: get_vm."""
    return [_brief_vm(v) for v in await ctx.api("GET", "/vms")]


@tool
async def get_vm(ctx: Ctx, vm: Ref) -> Any:
    """Full details of a VM: disks (targets), NICs (MACs, networks), CD-ROMs, boot order, IPs.

    Args:
        vm: VM name or id
    """
    v = await _find(ctx, "vms", vm)
    return await ctx.api("GET", f"/vms/{v['id']}")


@tool
async def create_vm(ctx: Ctx, name: str, cloud_image: Optional[Ref] = None, iso_path: Optional[str] = None,
                    memory_mb: int = 2048, vcpu: int = 2, disk_gb: int = 20, network: Optional[str] = None,
                    username: Optional[str] = None, password: Optional[str] = None,
                    ssh_keys: Optional[List[str]] = None, user_data: Optional[str] = None,
                    extra_nics: Optional[List[Dict[str, Any]]] = None, iommu: bool = False,
                    start: bool = True) -> Any:
    """Create a standalone VM from a cloud image (configured by cloud-init) or an ISO. For a VM inside a lab
    group use add_group_member instead (it gets a DHCP reservation and a DNS name).

    Args:
        name: VM name
        cloud_image: cloud image, e.g. 'debian-13' (distribution-version), its id or volume name (list_cloud_images)
        iso_path: ISO volume path to boot from (GET /storage/isos), instead of a cloud image
        memory_mb: memory in MiB
        vcpu: virtual CPUs
        disk_gb: boot disk size in GiB (cloud images are grown to it)
        network: libvirt network name (default: the app's default network)
        username: cloud-init login user
        password: cloud-init login password
        ssh_keys: SSH public keys for the login user
        user_data: raw #cloud-config (replaces username/password/ssh_keys)
        extra_nics: more NICs, e.g. [{"network": "lab2", "model": "virtio"}] (model igb for SR-IOV labs)
        iommu: add a virtual IOMMU (vfio / VF passthrough in the guest)
        start: start it right away
    """
    body: Dict[str, Any] = {"name": name, "memory": memory_mb, "vcpu": vcpu, "disk_size": disk_gb,
                            "iso_path": iso_path, "network_name": network, "cloudinit_username": username,
                            "cloudinit_password": password, "cloudinit_ssh_keys": ssh_keys or [],
                            "cloudinit_userdata": user_data, "extra_nics": extra_nics or [], "iommu": iommu,
                            "start": start}
    if cloud_image is not None:
        body["cloud_image_id"] = (await _cloud_image(ctx, cloud_image))["id"]
    return _brief_vm(await ctx.api("POST", "/vms", _clean(body)))


async def _cloud_image(ctx: Ctx, ref: Ref) -> Dict[str, Any]:
    images = await ctx.api("GET", "/storage/cloud-images")
    want = str(ref).lower()
    for img in images:  # same refs as group members: id, volume name or "<distribution>-<version>"
        slug = f"{img.get('distribution')}-{img.get('version')}".lower()
        if want in (str(img.get("id")), str(img.get("name", "")).lower(), slug):
            return img
    known = ", ".join(f"{i.get('distribution')}-{i.get('version')}" for i in images) or "none"
    raise ApiError(f"No cloud image '{ref}' (existing: {known})")


@tool
async def vm_power(ctx: Ctx, vm: Ref, action: Literal["start", "stop", "force_stop", "reboot", "suspend",
                                                      "resume"]) -> Any:
    """Power action on a VM. stop = ACPI shutdown, force_stop = pull the plug.

    Args:
        vm: VM name or id
        action: power action
    """
    v = await _find(ctx, "vms", vm)
    return _brief_vm(await ctx.api("POST", f"/vms/{v['id']}/{action}"))


@tool
async def delete_vm(ctx: Ctx, vm: Ref, delete_disks: bool = True) -> Any:
    """Delete a VM (irreversible). Lab group members should rather be removed with
    api_request DELETE /groups/{id}/members/{name}.

    Args:
        vm: VM name or id
        delete_disks: also delete its disk volumes and cloud-init seed
    """
    v = await _find(ctx, "vms", vm)
    return await ctx.api("DELETE", f"/vms/{v['id']}", query={"delete_disks": delete_disks})


@tool
async def add_disk(ctx: Ctx, vm: Ref, size_gb: int, bus: Literal["virtio", "sata"] = "virtio",
                   format: Literal["qcow2", "raw"] = "qcow2", pool: Optional[str] = None) -> Any:
    """Add a new empty disk to a VM (hot-plugged if it runs).

    Args:
        vm: VM name or id
        size_gb: size in GiB
        bus: virtio (vdX) or sata (sdX)
        format: image format
        pool: storage pool (default: the default pool)
    """
    v = await _find(ctx, "vms", vm)
    return await ctx.api("POST", f"/vms/{v['id']}/disks",
                         _clean({"size_gb": size_gb, "bus": bus, "format": format, "pool": pool}))


@tool
async def remove_disk(ctx: Ctx, vm: Ref, target: str, delete_volume: bool = False) -> Any:
    """Detach a disk from a VM (target as shown by get_vm, e.g. vdb). The boot disk can't be detached.

    Args:
        vm: VM name or id
        target: disk target, e.g. vdb
        delete_volume: also delete the disk's volume (otherwise it stays in its storage pool)
    """
    v = await _find(ctx, "vms", vm)
    return await ctx.api("DELETE", f"/vms/{v['id']}/disks/{target}", query={"delete_volume": delete_volume})


@tool
async def add_nic(ctx: Ctx, vm: Ref, network: str, model: str = "virtio", mac: Optional[str] = None,
                  vlan: Optional[int] = None) -> Any:
    """Add a network card to a VM (hot-plugged if it runs). Lab group networks are named vmm-g-<group>.

    Args:
        vm: VM name or id
        network: libvirt network name (list_networks)
        model: virtio, e1000e, igb (SR-IOV labs)...
        mac: fixed MAC address (libvirt picks one if omitted)
        vlan: VLAN tag (SR-IOV VF pool networks only)
    """
    v = await _find(ctx, "vms", vm)
    return await ctx.api("POST", f"/vms/{v['id']}/nics",
                         _clean({"network": network, "model": model, "mac": mac, "vlan": vlan}))


@tool
async def remove_nic(ctx: Ctx, vm: Ref, mac: str) -> Any:
    """Remove a network card from a VM.

    Args:
        vm: VM name or id
        mac: MAC address of the NIC (get_vm)
    """
    v = await _find(ctx, "vms", vm)
    return await ctx.api("DELETE", f"/vms/{v['id']}/nics/{mac}")


# ---------------------------------------------------------------------------------------------- run commands

async def _vm_name(ctx: Ctx, vm: Ref) -> str:
    return (await _find(ctx, "vms", vm))["name"]


def _agent_exec(name: str, argv: List[str], stdin: Optional[str], timeout: float) -> Dict[str, Any]:
    from app.libvirt_client import libvirt_client
    try:
        return libvirt_client.agent_exec(name, argv[0], argv[1:], stdin.encode() if stdin is not None else None,
                                         timeout=timeout)
    except libvirt.libvirtError as e:
        raise ApiError(f"{name}: guest agent: {e.get_error_message() or e} (is the VM running with "
                       f"qemu-guest-agent? EL images only allow guest-exec on cluster nodes / routers: use ssh_exec)")
    except TimeoutError as e:
        raise ApiError(str(e))


@tool(admin=True)
async def vm_exec(ctx: Ctx, vm: Ref, command: str, stdin: Optional[str] = None, timeout: int = 120) -> Any:
    """Run a shell command as root inside a VM through the QEMU guest agent: no network, SSH key or
    WireGuard needed (works on isolated lab networks, routers, cluster nodes). Runs `/bin/sh -c command`.

    Args:
        vm: VM name or id (group members are <group>-<member>, routers <group>-rtr)
        command: shell command, e.g. 'ip -br a; systemctl --failed'
        stdin: text passed on standard input (e.g. a manifest for `kubectl apply -f -`)
        timeout: seconds to wait
    """
    ctx.require_admin()
    name = await _vm_name(ctx, vm)
    out = await run_in_threadpool(_agent_exec, name, ["/bin/sh", "-c", command], stdin, timeout)
    return {"vm": name, **out}


@tool(admin=True)
async def vm_read_file(ctx: Ctx, vm: Ref, path: str) -> Any:
    """Read a text file inside a VM through the guest agent (e.g. /etc/kubernetes/admin.conf,
    /var/log/cloud-init-output.log).

    Args:
        vm: VM name or id
        path: absolute path in the guest
    """
    ctx.require_admin()
    name = await _vm_name(ctx, vm)
    out = await run_in_threadpool(_agent_exec, name, ["/bin/cat", "--", path], None, 30)
    if out["exitcode"] != 0:
        raise ApiError(f"{name}:{path}: {out['stderr'].strip()}")
    return out["stdout"][:MAX_TEXT]


@tool(admin=True)
async def vm_write_file(ctx: Ctx, vm: Ref, path: str, content: str, mode: Optional[str] = None) -> Any:
    """Write a file inside a VM through the guest agent (replaces it).

    Args:
        vm: VM name or id
        path: absolute path in the guest
        content: file content
        mode: optional chmod mode, e.g. '0600' or '0755'
    """
    ctx.require_admin()
    name = await _vm_name(ctx, vm)
    script = 'cat > "$1"' + (' && chmod "$2" "$1"' if mode else "")
    argv = ["/bin/sh", "-c", script, "sh", path] + ([mode] if mode else [])
    out = await run_in_threadpool(_agent_exec, name, argv, content, 60)
    if out["exitcode"] != 0:
        raise ApiError(f"{name}:{path}: {out['stderr'].strip()}")
    return {"vm": name, "path": path, "bytes": len(content.encode())}


@tool(admin=True)
async def ssh_exec(ctx: Ctx, host: str, command: str, user: Optional[str] = None, port: int = 22,
                   jump: Optional[str] = None, timeout: int = 120) -> Any:
    """Run a command over SSH from the vm-manager host, with the service account's SSH keys (no host key
    check: lab VMs are re-created often). For VMs reachable from the host (NAT networks, router uplinks);
    group members on isolated networks: use vm_exec, or jump through the router.

    Args:
        host: IP address or name
        command: remote command
        user: remote user (default: the service account name)
        port: SSH port
        jump: ProxyJump host, e.g. 'admin@192.168.122.50'
        timeout: seconds
    """
    ctx.require_admin()
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10", "-p", str(port)]
    if jump:
        argv += ["-J", jump]
    argv += [f"{user}@{host}" if user else host, command]
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ApiError(f"ssh {host}: timed out after {timeout} s")
    return {"exitcode": proc.returncode, "stdout": stdout.decode(errors="replace")[:MAX_TEXT],
            "stderr": stderr.decode(errors="replace")[:MAX_TEXT]}


# ---------------------------------------------------------------------------------------------- lab groups

@tool
async def list_groups(ctx: Ctx) -> Any:
    """List lab groups (isolated network + router VM with DNS/DHCP/NAT + member VMs)."""
    return await ctx.api("GET", "/groups")


@tool
async def get_group(ctx: Ctx, group: Ref, leases: bool = True) -> Any:
    """Lab group details: spec (members, IPs, DNS records, WireGuard, BGP, egress, registry), VMs, status.

    Args:
        group: group name or id
        leases: also read the router's current DHCP leases
    """
    g = await _find(ctx, "groups", group)
    detail = await ctx.api("GET", f"/groups/{g['id']}")
    if leases:
        try:
            detail["leases"] = await ctx.api("GET", f"/groups/{g['id']}/leases")
        except ApiError as e:
            detail["leases"] = str(e)
    return detail


@tool
async def create_group(ctx: Ctx, spec: Dict[str, Any]) -> Any:
    """Create a lab group from a full GroupSpec (runs as a task; follow it with wait_for_task).
    Call api_describe(path_contains='/groups', method='POST') for the exact schema. Minimal example:
    {"name": "case123", "cidr": "10.42.7.0/24", "domain": "lab", "members": [{"name": "web", "image": "debian-13"}]}
    (cidr: a free /24, see list_groups / list_networks).
    Often easier: list_templates + create_from_template.

    Args:
        spec: the GroupSpec JSON object
    """
    return await ctx.api("POST", "/groups", spec)


@tool
async def add_group_member(ctx: Ctx, group: Ref, member: Dict[str, Any]) -> Any:
    """Add a VM to a lab group (gets a DHCP reservation and <name>.<domain>). MemberSpec example:
    {"name": "db", "image": "almalinux-9", "memory": 2048, "vcpus": 2, "disk_size": 20}; source "iso" or
    "empty" for non cloud-init machines (api_describe '/groups/{group_id}/members' for every field).

    Args:
        group: group name or id
        member: MemberSpec JSON object
    """
    g = await _find(ctx, "groups", group)
    return await ctx.api("POST", f"/groups/{g['id']}/members", member)


@tool
async def wireguard_device(ctx: Ctx, group: Ref, device: str, endpoint_host: Optional[str] = None) -> Any:
    """Give a device (the user's laptop) VPN access to a lab group: enables WireGuard on the group router if
    needed, creates the peer and returns its wg-quick config plus ready-to-run commands to install it.
    If you have a shell on the user's machine, run `linux_nmcli` (or `wg_quick`) there to connect
    (ask the user first); otherwise give them the file to import in the WireGuard app.

    Args:
        group: group name or id
        device: device name (lowercase letters, digits, dashes), e.g. 'laptop'
        endpoint_host: host name / address the device reaches vm-manager's host on (default: the host's LAN address)
    """
    g = await _find(ctx, "groups", group)
    status = await ctx.api("GET", f"/groups/{g['id']}/wireguard")
    if not status.get("enabled"):
        await ctx.api("PUT", f"/groups/{g['id']}/wireguard", {"enabled": True})
    created = await ctx.api("POST", f"/groups/{g['id']}/wireguard/peers",
                            _clean({"name": device, "endpoint_host": endpoint_host}))
    filename = created.get("filename") or f"{g['name']}.conf"
    conn = filename[:-5] if filename.endswith(".conf") else filename
    conf = created["config"].rstrip("\n")
    return {
        "config": conf,
        "filename": filename,
        "warning": created.get("warning"),
        "linux_nmcli": (f"umask 077; cat > /tmp/{filename} <<'EOF'\n{conf}\nEOF\n"
                        f"nmcli connection import type wireguard file /tmp/{filename} && rm -f /tmp/{filename}\n"
                        f"nmcli connection up {conn}"),
        "wg_quick": (f"sudo install -m 600 /dev/stdin /etc/wireguard/{filename} <<'EOF'\n{conf}\nEOF\n"
                     f"sudo wg-quick up {conn}"),
        "disconnect": f"nmcli connection down {conn}   # or: sudo wg-quick down {conn}",
        "macos_windows": f"Save the config as {filename} and use 'Import tunnel(s) from file' in the WireGuard app",
    }


# ---------------------------------------------------------------------------------------------- clusters

@tool
async def list_clusters(ctx: Ctx) -> Any:
    """List Kubernetes / OpenShift clusters (type, status, nodes, API endpoint, console URL)."""
    return await ctx.api("GET", "/clusters")


@tool
async def get_cluster(ctx: Ctx, cluster: Ref) -> Any:
    """Cluster details (nodes with IPs and VM states, API endpoint, task progress); for OpenShift also the
    install status (phase, cluster operators, add-ons).

    Args:
        cluster: cluster name or id
    """
    c = await _find(ctx, "clusters", cluster)
    detail = await ctx.api("GET", f"/clusters/{c['id']}")
    if detail.get("type") == "openshift":
        try:
            detail["install_status"] = await ctx.api("GET", f"/clusters/{c['id']}/openshift/install-status")
        except ApiError as e:
            detail["install_status"] = str(e)
    return detail


@tool
async def create_cluster(ctx: Ctx, spec: Dict[str, Any]) -> Any:
    """Create a k3s, kubeadm or OpenShift cluster (runs as a task: follow it with wait_for_cluster).
    Exact schema: api_describe(path_contains='/clusters', method='POST'). Examples:
    - k3s: {"name": "k3", "type": "k3s", "workers": 2}
    - kubeadm HA + MetalLB BGP: {"name": "k8s", "type": "kubeadm", "version": "v1.34", "ctlplanes": 3,
      "workers": 2, "kubeadm": {"metallb": {"enabled": true, "mode": "bgp", "bfd": true}}}
    - OpenShift SNO with LVMS and an operator: {"name": "sno", "type": "openshift", "openshift":
      {"topology": "sno", "storage": "lvms", "operators": [{"name": "kubernetes-nmstate-operator"}]}}
      (needs the pull secret: GET/PUT /openshift/pull-secret; versions: GET /openshift/versions)
    - disconnected: kubeadm "disconnected": true; OpenShift "openshift": {"disconnected": true}
    kubeadm / OpenShift go into a lab group: "group_id" of an existing one, else one is created.

    Args:
        spec: ClusterCreate JSON object
    """
    return await ctx.api("POST", "/clusters", spec)


@tool
async def cluster_power(ctx: Ctx, cluster: Ref, action: Literal["start", "stop"]) -> Any:
    """Start (control planes first, waits for Ready nodes) or stop (workers first) a cluster.

    Args:
        cluster: cluster name or id
        action: start or stop
    """
    c = await _find(ctx, "clusters", cluster)
    return await ctx.api("POST", f"/clusters/{c['id']}/{action}")


@tool
async def get_kubeconfig(ctx: Ctx, cluster: Ref) -> Any:
    """The cluster's admin kubeconfig (YAML). Its server is the router's uplink address: reachable from the
    vm-manager host, and from a laptop through the group's WireGuard (wireguard_device).

    Args:
        cluster: cluster name or id
    """
    c = await _find(ctx, "clusters", cluster)
    return await ctx.api("GET", f"/clusters/{c['id']}/kubeconfig")


def _cluster_orm(cluster_id: int):
    from app.database import SessionLocal
    from app.models import Cluster
    db = SessionLocal()
    cluster = db.get(Cluster, cluster_id)
    if cluster is None:
        db.close()
        raise ApiError(f"Cluster {cluster_id} not found")
    return db, cluster


def _kubectl(cluster_id: int, args: List[str], stdin: Optional[str], timeout: float) -> Dict[str, Any]:
    from app.services.kube_exec import KubeExecError, run_kubectl
    db, cluster = _cluster_orm(cluster_id)
    try:
        return run_kubectl(cluster, args, stdin, timeout)
    except KubeExecError as e:
        raise ApiError(str(e))
    finally:
        db.close()


@tool(admin=True)
async def kubectl(ctx: Ctx, cluster: Ref, args: str, stdin: Optional[str] = None, timeout: int = 120) -> Any:
    """Run kubectl against a cluster, as cluster admin. kubeadm / k3s: on the first running control plane
    through the guest agent; OpenShift: the app's oc on the host (prefer the `oc` tool there: must-gather,
    inspect, debug node, oc-specific checks). No local kubectl, kubeconfig or VPN needed. Apply manifests with
    args 'apply -f -' and the YAML in stdin.

    Args:
        cluster: cluster name or id
        args: arguments, e.g. 'get pods -A -o wide' or 'get csv -n openshift-nmstate'
        stdin: standard input (manifests for apply -f -)
        timeout: seconds
    """
    ctx.require_admin()
    c = await _find(ctx, "clusters", cluster)
    return await run_in_threadpool(_kubectl, c["id"], shlex.split(args), stdin, timeout)


def _oc_now(cluster_id: int, argv: List[str], stdin: Optional[str], timeout: float) -> Dict[str, Any]:
    from app.services.kube_exec import KubeExecError, run_oc
    db, cluster = _cluster_orm(cluster_id)
    try:
        return run_oc(db, cluster, argv, stdin, timeout)
    except KubeExecError as e:
        raise ApiError(str(e))
    finally:
        db.close()


def _oc_task(cluster_id: int, argv: List[str], kind: str) -> int:
    """adm must-gather / inspect as a task (they can take 10+ min); checks first, so a refusal is immediate"""
    from app.schemas import TaskCreate
    from app.services.kube_exec import GATHER_TIMEOUT, KubeExecError, prepare_oc, run_oc
    from app.services.task_service import task_service
    db, cluster = _cluster_orm(cluster_id)
    try:
        prepared = prepare_oc(db, cluster, argv)

        def body(session, task):
            from app.models import Cluster
            out = run_oc(session, session.get(Cluster, cluster_id), argv, None, GATHER_TIMEOUT,
                         lambda: task_service.is_cancelled(task.id), prepared)
            if task_service.is_cancelled(task.id):
                raise RuntimeError("cancelled: " + "; ".join(out.get("cleanup") or []))
            if out["exitcode"] != 0 and "archive" not in out:
                raise RuntimeError(f"oc adm {kind} failed (exit {out['exitcode']}): "
                                   f"{(out['stderr'] or out['stdout']).strip()[-1500:]}")
            return out

        task = task_service.start(db, TaskCreate(name=f"oc adm {kind} on {cluster.name}", type="oc",
                                                 description="oc " + shlex.join(prepared["argv"]),
                                                 target_type="cluster", target_id=cluster.id,
                                                 target_name=cluster.name), body)
        return task.id
    except KubeExecError as e:
        raise ApiError(str(e))
    finally:
        db.close()


@tool(admin=True)
async def oc(ctx: Ctx, cluster: Ref, args: str, stdin: Optional[str] = None, timeout: int = 120) -> Any:
    """Run `oc` against an OpenShift cluster as cluster admin, like at a customer: the app's oc for the
    cluster's version, from the vm-manager host (no login, kubeconfig or VPN needed). Non-interactive: no
    `login`, `edit`, `-it`, shell without a command, `-f` / `--watch` (the tool says what to run instead).
    For kubeadm / k3s clusters use `kubectl`.

    Examples (args):
      health: 'get clusterversion', 'get co', 'get nodes -o wide', 'get csv -A', 'get mcp',
        'adm top nodes', 'adm top pods -A --sort-by=memory', 'get events -A --sort-by=.lastTimestamp',
        'whoami --show-console' (console URL), 'adm upgrade'
      a problem: 'describe co/ingress', 'describe pod <pod> -n <ns>', 'logs <pod> -n <ns> --tail=200',
        'logs <pod> -n <ns> -c <container> --previous', 'logs deploy/<name> -n <ns> --all-containers --since=10m',
        'adm node-logs <node> -u kubelet --tail=200'
      inside: 'exec <pod> -n <ns> -- cat /etc/resolv.conf', 'rsh -n <ns> <pod> curl -s localhost:8080/healthz',
        'debug node/<node> -- chroot /host journalctl -u crio --since -15min --no-pager'
        ('chroot /host crictl ps', 'chroot /host ip -br a'; the tools image: mirrored on disconnected clusters)
      apply: 'apply -f -' with the YAML in stdin; 'patch <kind> <name> -n <ns> --type merge -p '{"spec":{...}}''
      support data: 'adm inspect co/kube-apiserver' or 'adm inspect ns/<ns>' (a few seconds to minutes),
        'adm must-gather' (5-15 min; '-- /usr/bin/gather_audit_logs' for audit logs, '--image=<operator
        must-gather image>' for an operator's). These run as a task: the result (directory listing + tarball
        path on the host + download link) comes back when done within `timeout`, else a task id for
        wait_for_task. On a disconnected cluster the image must be in the mirror registry: checked first.
    Big outputs are cut in the middle (narrow with -n, -l, -o jsonpath=..., --tail). Files written by
    oc cp / rsync land in the install dir's oc-output (GET /clusters/{id}/oc-output lists them).

    Args:
        cluster: OpenShift cluster name or id
        args: oc arguments (shell quoting), e.g. 'get co' or 'logs -n openshift-dns ds/dns-default --tail=50'
        stdin: standard input (manifests for apply -f -, input for exec -i)
        timeout: seconds; for adm must-gather / inspect: how long to wait for the task before returning its id
    """
    ctx.require_admin()
    c = await _find(ctx, "clusters", cluster)
    try:
        argv = shlex.split(args)
    except ValueError as e:
        raise ApiError(f"args: {e} (shell quoting: wrap JSON in single quotes)")
    from app.services.kube_exec import oc_gather_kind
    kind = oc_gather_kind(argv[1:] if argv[:1] == ["oc"] else argv)
    if not kind:
        return await run_in_threadpool(_oc_now, c["id"], argv, stdin, timeout)
    task_id = await run_in_threadpool(_oc_task, c["id"], argv, kind)
    from app.services.task_service import task_service
    await run_in_threadpool(task_service.wait, task_id, min(timeout, 1800))
    task = await ctx.api("GET", f"/tasks/{task_id}")
    if task.get("status") == "completed":
        return task.get("result")
    if task.get("status") in ("failed", "cancelled"):
        raise ApiError(task.get("error_message") or f"oc adm {kind} {task.get('status')}")
    return {"task_id": task_id, "still_running": True,
            "hint": f"oc adm {kind} is still running: wait_for_task({task_id}) returns the listing and tarball "
                    f"(must-gather takes 5-15 min)"}


KUBEADM_FILES = {
    "admin.conf": ["/bin/cat", "/etc/kubernetes/admin.conf"],
    "init-config": ["/bin/cat", "/etc/vm-manager/kubeadm-init.yaml.rendered"],
    "cluster-config": ["/usr/bin/kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf", "-n", "kube-system",
                       "get", "configmap", "kubeadm-config", "-o", "jsonpath={.data.ClusterConfiguration}"],
    "kubelet-config": ["/bin/cat", "/var/lib/kubelet/config.yaml"],
    "ca.crt": ["/bin/cat", "/etc/kubernetes/pki/ca.crt"],
    "join-command": ["/usr/bin/kubeadm", "token", "create", "--print-join-command"],
    "static-pods": ["/bin/sh", "-c", "for f in /etc/kubernetes/manifests/*.yaml; do echo \"# $f\"; cat \"$f\"; done"],
    "certs-expiration": ["/usr/bin/kubeadm", "certs", "check-expiration"],
}


@tool(admin=True)
async def kubeadm_files(ctx: Ctx, cluster: Ref,
                        files: Optional[List[Literal["admin.conf", "init-config", "cluster-config",
                                                     "kubelet-config", "ca.crt", "join-command", "static-pods",
                                                     "certs-expiration"]]] = None) -> Any:
    """Extract kubeadm files from a kubeadm cluster's first control plane: admin.conf, the kubeadm init
    config the app used (init-config, contains the bootstrap token), the live ClusterConfiguration,
    kubelet config, CA certificate, a fresh join command, the static pod manifests, certificate expiry.
    Any other file: vm_read_file.

    Args:
        cluster: kubeadm cluster name or id
        files: which ones (default: init-config, cluster-config, kubelet-config, join-command)
    """
    ctx.require_admin()
    c = await _find(ctx, "clusters", cluster)
    if c.get("type") != "kubeadm":
        raise ApiError(f"{c['name']} is a {c.get('type')} cluster, not kubeadm (use kubectl / vm_read_file)")
    wanted = files or ["init-config", "cluster-config", "kubelet-config", "join-command"]

    def run() -> Dict[str, Any]:
        from app.services.cluster_service import cluster_service
        db, orm = _cluster_orm(c["id"])
        try:
            node = cluster_service._api_node(orm)
            if node is None:
                raise ApiError("The cluster has no control plane")
            result: Dict[str, Any] = {"node": node.name}
            for f in wanted:
                out = _agent_exec(node.name, KUBEADM_FILES[f], None, 60)
                result[f] = out["stdout"] if out["exitcode"] == 0 else {"error": out["stderr"].strip()}
            return result
        finally:
            db.close()
    return await run_in_threadpool(run)


@tool
async def install_cluster_addon(ctx: Ctx, cluster: Ref,
                                kind: Literal["operator", "lvms", "odf", "sriov", "metallb", "metallb-demo"],
                                operator: Optional[str] = None, channel: Optional[str] = None,
                                source: Optional[str] = None, namespace: Optional[str] = None,
                                options: Optional[Dict[str, Any]] = None) -> Any:
    """Day-2 add-on on an installed OpenShift cluster (task): an OLM operator (Namespace + OperatorGroup +
    Subscription, and its CR when the app knows it), LVMS / ODF storage, SR-IOV operator, MetalLB (+ demo).
    Find package names with search_operators. Disconnected clusters mirror the operator first.
    kubeadm MetalLB: api_request PUT /clusters/{id}/metallb.

    Args:
        cluster: OpenShift cluster name or id
        kind: add-on kind
        operator: OLM package name when kind=operator, e.g. 'kubernetes-nmstate-operator'
        channel: operator channel (default: the package's default channel)
        source: catalog source (default redhat-operators; certified-operators, community-operators...)
        namespace: target namespace (default: suggested one)
        options: kind=sriov: SriovOptions, kind=metallb: MetalLBOptions (api_describe '/openshift/addons')
    """
    c = await _find(ctx, "clusters", cluster)
    body: Dict[str, Any] = {"kind": kind}
    if kind == "operator":
        if not operator:
            raise ApiError("operator (package name) is required when kind=operator")
        body["operator"] = _clean({"name": operator, "channel": channel, "source": source, "namespace": namespace})
    elif options and kind in ("sriov", "metallb"):
        body[kind] = options
    return await ctx.api("POST", f"/clusters/{c['id']}/openshift/addons", body)


@tool
async def search_operators(ctx: Ctx, query: str = "", cluster: Optional[Ref] = None) -> Any:
    """Find OLM operators. With a cluster: its live catalogs (package, channels, source); without: the
    curated list offered at OpenShift install time. Also returns the operators already installed.

    Args:
        query: substring of the package / display name, e.g. 'nmstate', 'cert-manager'
        cluster: OpenShift cluster name or id
    """
    q = query.lower()

    def match(p: Dict[str, Any]) -> bool:
        return not q or q in json.dumps({k: p.get(k) for k in ("name", "display_name", "description")}).lower()
    if cluster is None:
        return [p for p in await ctx.api("GET", "/openshift/catalog") if match(p)]
    c = await _find(ctx, "clusters", cluster)
    packages = await ctx.api("GET", f"/clusters/{c['id']}/openshift/packagemanifests")
    installed = await ctx.api("GET", f"/clusters/{c['id']}/openshift/operators")
    return {"matches": [p for p in packages if match(p)][:50], "installed": installed}


# ---------------------------------------------------------------------------------------------- images, networks, templates

@tool
async def list_cloud_images(ctx: Ctx) -> Any:
    """Cloud images usable for VMs, group members and cluster nodes, plus the distributions that can be
    downloaded (download one with api_request POST /storage/cloud-images {distribution, version})."""
    images = await ctx.api("GET", "/storage/cloud-images")
    distributions = await ctx.api("GET", "/storage/cloud-images/distributions")
    return {"images": [{k: i.get(k) for k in ("id", "name", "distribution", "version", "status")} for i in images],
            "downloadable": distributions}


@tool
async def list_networks(ctx: Ctx) -> Any:
    """libvirt networks (name, bridge, CIDR, forward mode, DHCP range, active). Group networks are vmm-g-<group>.
    Full XML: api_request GET /networks/{id}."""
    return [{k: v for k, v in n.items() if k != "xml_config"} for n in await ctx.api("GET", "/networks")]


@tool
async def list_templates(ctx: Ctx) -> Any:
    """Customer-case templates (lab group + optional cluster): id, title, parameters with defaults."""
    return await ctx.api("GET", "/templates")


@tool
async def create_from_template(ctx: Ctx, template: str, params: Optional[Dict[str, Any]] = None,
                               dry_run: bool = False) -> Any:
    """Create a lab from a template (group task + cluster task if the template has a cluster).
    dry_run=True renders it instead: resulting YAML, host checks, resource estimate, OpenTofu HCL.

    Args:
        template: template id (list_templates)
        params: template parameters (list_templates shows each template's), e.g. {"case": "42"}
        dry_run: only render / check
    """
    action = "render" if dry_run else "create"
    return await ctx.api("POST", f"/templates/{template}/{action}", {"params": params or {}})


# ---------------------------------------------------------------------------------------------- tasks / waiting

@tool
async def list_tasks(ctx: Ctx, status: Optional[Literal["pending", "running", "completed", "failed",
                                                         "cancelled"]] = None, limit: int = 20) -> Any:
    """Recent background tasks (downloads, group / cluster creation, add-ons...).

    Args:
        status: filter on status
        limit: how many
    """
    tasks = await ctx.api("GET", "/tasks")
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    return tasks[:limit]


@tool
async def wait_for_task(ctx: Ctx, task_id: int, timeout: int = 600) -> Any:
    """Wait until a task ends (completed / failed / cancelled) or the timeout passes; returns the task with
    its progress and messages. Call again to keep waiting.

    Args:
        task_id: task id
        timeout: seconds to wait at most (max 1800)
    """
    deadline = asyncio.get_running_loop().time() + min(timeout, 1800)
    while True:
        task = await ctx.api("GET", f"/tasks/{task_id}")
        if task.get("status") in ("completed", "failed", "cancelled"):
            return task
        if asyncio.get_running_loop().time() >= deadline:
            return {"still_running": True, **task}
        await asyncio.sleep(5)


@tool
async def wait_for_cluster(ctx: Ctx, cluster: Ref, timeout: int = 900) -> Any:
    """Wait until a cluster leaves its creating / installing state (ready or error) or the timeout passes.
    OpenShift installs take 40-90 min: call it repeatedly.

    Args:
        cluster: cluster name or id
        timeout: seconds to wait at most (max 1800)
    """
    c = await _find(ctx, "clusters", cluster)
    deadline = asyncio.get_running_loop().time() + min(timeout, 1800)
    while True:
        detail = await ctx.api("GET", f"/clusters/{c['id']}")
        if not detail.get("task_running") and detail.get("status") not in ("creating", "installing", "pending"):
            return {k: detail.get(k) for k in ("id", "name", "status", "status_message", "api_endpoint",
                                                "console_url", "nodes")}
        if asyncio.get_running_loop().time() >= deadline:
            return {"still_running": True, **{k: detail.get(k) for k in ("status", "status_message",
                                                                          "task_progress", "task_id")}}
        await asyncio.sleep(10)


# ---------------------------------------------------------------------------------------------- logs / diagnostics

def _compact_logs(r: Any) -> Any:
    """Shorter log answers for a model: no empty context, per-category rule counts only"""
    if not isinstance(r, dict):
        return r
    for e in r.get("entries", []):
        for k in ("before", "after", "rules"):
            if not e.get(k):
                e.pop(k, None)
        if e.get("level") == "info":
            e.pop("level")
    for s in r.get("summary", []):
        s["rules"] = {x["id"]: x["count"] for x in s.get("rules", [])}
    return r


@tool(admin=True)
async def vm_logs(ctx: Ctx, vm: Ref, source: Literal["journal", "dmesg", "cloud-init", "file"] = "journal",
                  lines: int = 1000, unit: Optional[List[str]] = None, since: Optional[str] = None,
                  boot: Optional[str] = None, grep: Optional[str] = None, path: Optional[str] = None,
                  level: Optional[Literal["error", "warning"]] = None, include: Optional[str] = None,
                  exclude: Optional[str] = None, problems_only: bool = False, category: Optional[str] = None,
                  context: int = 1, max_entries: int = 200) -> Any:
    """Read a VM's logs through the guest agent (no SSH) and filter them on the server: known-problem rules (OOM,
    segfault, I/O errors, link down, firmware, SELinux AVC, x509, DNS, connection refused, SR-IOV / VFIO / IOMMU,
    cloud-init, failed units...) give a category + plain-words hint per matched line and counts per category
    (`summary`). Typical: problems_only=true, or level='error'. Logs may hold secrets (admins only).

    Args:
        vm: VM name or id
        source: journal (journalctl), dmesg (kernel), cloud-init (status --long + its logs), file (any path)
        lines: lines fetched from the source (tail, max 20000)
        unit: journal: systemd units, e.g. ['kubelet', 'crio']
        since: journal / dmesg: '-1h', 'today', '2026-10-11 10:00'
        boot: journal / dmesg: '0' current boot, '-1' previous, 'all'
        grep: journal: journalctl --grep in the guest
        path: file: absolute path in the VM, e.g. /var/log/messages
        level: keep lines of this level or worse
        include: regex the lines must match (case-insensitive)
        exclude: regex of lines to drop
        problems_only: only lines matching a known-problem rule
        category: comma-separated categories or rule ids (see summary), e.g. 'memory,storage'
        context: lines of context around each kept line (0-20)
        max_entries: max lines returned (most recent kept)
    """
    v = await _find(ctx, "vms", vm)
    return _compact_logs(await ctx.api("GET", f"/vms/{v['id']}/logs", query={
        "source": source, "lines": lines, "unit": unit, "since": since, "boot": boot, "grep": grep, "path": path,
        "level": level, "include": include, "exclude": exclude, "problems_only": problems_only,
        "category": category, "context": context, "max_entries": max_entries}))


@tool
async def vm_diagnostics(ctx: Ctx, vm: Ref) -> Any:
    """Inventory of a VM through the guest agent, for a support case: OS / kernel / cmdline, BIOS and system
    (dmidecode: firmware versions), lspci -nnk (drivers), NICs (driver, firmware-version, bus-info, addresses, VFs),
    disks, file systems, SR-IOV PFs / IOMMU groups / vfio devices, failed systemd units, and `findings` in plain words.

    Args:
        vm: VM name or id
    """
    v = await _find(ctx, "vms", vm)
    return await ctx.api("GET", f"/vms/{v['id']}/diagnostics")


@tool
async def find_pods(ctx: Ctx, cluster: Ref, match: Optional[str] = None, namespace: Optional[str] = None,
                    selector: Optional[str] = None, problems_only: bool = False) -> Any:
    """Find pods in a cluster (k3s / kubeadm / OpenShift) with their phase, restarts and problems
    (CrashLoopBackOff, ImagePullBackOff, OOMKilled...), e.g. match='sriov' for the SR-IOV operator's pods.

    Args:
        cluster: cluster name or id
        match: substring of 'namespace/name' (case-insensitive)
        namespace: exact namespace
        selector: label selector, e.g. 'app=sriov-network-config-daemon'
        problems_only: only pods with a waiting / failed container
    """
    c = await _find(ctx, "clusters", cluster)
    return await ctx.api("GET", f"/clusters/{c['id']}/pods", query={
        "match": match, "namespace": namespace, "selector": selector, "problems_only": problems_only})


@tool(admin=True)
async def pod_logs(ctx: Ctx, cluster: Ref, match: Optional[str] = None, namespace: Optional[str] = None,
                   pod: Optional[str] = None, selector: Optional[str] = None, container: Optional[str] = None,
                   previous: bool = False, tail: int = 5000, since: Optional[str] = None,
                   level: Optional[Literal["error", "warning"]] = None, include: Optional[str] = None,
                   exclude: Optional[str] = None, problems_only: bool = False, category: Optional[str] = None,
                   context: int = 1, max_entries: int = 200) -> Any:
    """Logs of one pod or of every pod matching a name substring / namespace / selector (up to 30 pods, all
    containers, lines prefixed [ns/pod/container]) plus their events and container status lines, filtered on the
    server with the known-problem rules (summary = counts per category with a hint). "Logs of the sriov pods with
    the common errors" = pod_logs(cluster, match='sriov', problems_only=true).

    Args:
        cluster: cluster name or id
        match: pods whose 'namespace/name' contains this (case-insensitive)
        namespace: namespace (needed with pod)
        pod: one pod name
        selector: label selector
        container: only this container
        previous: logs of the previous (crashed) container instance
        tail: lines per container
        since: e.g. '15m', '2h'
        level: keep lines of this level or worse
        include: regex the lines must match
        exclude: regex of lines to drop
        problems_only: only lines matching a known-problem rule
        category: comma-separated categories or rule ids, e.g. 'sriov,images'
        context: lines of context around each kept line
        max_entries: max lines returned (most recent kept)
    """
    c = await _find(ctx, "clusters", cluster)
    return _compact_logs(await ctx.api("GET", f"/clusters/{c['id']}/logs", query={
        "match": match, "namespace": namespace, "pod": pod, "selector": selector, "container": container,
        "previous": previous, "tail": tail, "since": since, "level": level, "include": include, "exclude": exclude,
        "problems_only": problems_only, "category": category, "context": context, "max_entries": max_entries},
        timeout=600))
