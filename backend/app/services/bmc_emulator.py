"""BMC emulator process of one lab group: sushy-tools (Redfish, libvirt driver) restricted to the group's BMC members.

Run by bmc_service as `python -m app.services.bmc_emulator <config.json>` (one process per group, own port).
Only stdlib + sushy_tools + libvirt here: no app settings / DB import.

config.json: {listen, port, libvirt_uri, auth_file (htpasswd, bcrypt), allow_file (JSON list of VM names),
state_dir, hosts: {"<zone>": "<ip>"}, redirects: {"<group cidr>": "<router uplink ip>"}, redirect_ports, cert?, key?}
- allow_file is re-read when it changes: the allowed identities are those VM names and their UUIDs (Redfish system
  ids); everything else is "not found", in the systems list too.
- hosts: names under <zone> (e.g. a hub cluster's api / *.apps) resolve to <ip> (its router's uplink address) in
  this process only: virtual media URLs of a hub (assisted-image-service route) are downloaded by this process, and
  the host itself can't resolve lab names.
"""
import json
import os
import socket
import sys
import threading
import time


def _load(path):
    with open(path) as f:
        return json.load(f)


def _patch_resolver(hosts, redirects, ports):
    """getaddrinfo: names under the configured zones -> the zone's address; addresses of a lab group network on
    `ports` (a hub's Ironic image server on its nodes' host network) -> the group router's uplink address (the
    router forwards those ports; the host has no route to group networks)"""
    import ipaddress
    real = socket.getaddrinfo
    zones = sorted(((z.lower().rstrip("."), ip) for z, ip in (hosts or {}).items()), key=lambda x: -len(x[0]))
    nets = [(ipaddress.ip_network(c), ip) for c, ip in (redirects or {}).items()]
    ports = {int(p) for p in ports or []}

    def getaddrinfo(host, port, *args, **kwargs):
        if isinstance(host, str):
            name = host.lower().rstrip(".")
            for zone, ip in zones:
                if name == zone or name.endswith("." + zone):
                    return real(ip, port, *args, **kwargs)
            try:
                addr = ipaddress.ip_address(name.strip("[]"))
            except ValueError:
                addr = None
            if addr is not None and port is not None and int(port) in ports:
                for net, ip in nets:
                    if addr in net:
                        return real(ip, port, *args, **kwargs)
        return real(host, port, *args, **kwargs)
    socket.getaddrinfo = getaddrinfo


class _Allowed:
    """VM names from allow_file + their UUIDs (looked up through libvirt, cached 15 s)"""

    def __init__(self, path, uri):
        self.path, self.uri = path, uri
        self.lock = threading.Lock()
        self.mtime, self.names, self.ids, self.at = None, [], set(), 0.0

    def get(self):
        with self.lock:
            try:
                mtime = os.stat(self.path).st_mtime
            except OSError:
                return set()
            if mtime != self.mtime:
                try:
                    self.names = list(_load(self.path))
                except (OSError, ValueError):
                    self.names = []
                self.mtime, self.at = mtime, 0.0
            if time.monotonic() - self.at > 15:
                ids = set(self.names)
                try:
                    import libvirt
                    conn = libvirt.openReadOnly(self.uri)
                    try:
                        for name in self.names:
                            try:
                                ids.add(conn.lookupByName(name).UUIDString())
                            except libvirt.libvirtError:
                                pass
                    finally:
                        conn.close()
                except Exception:  # libvirt down: names only
                    pass
                self.ids, self.at = ids, time.monotonic()
            return self.ids


_ACTION_PATHS = ("/Actions/ComputerSystem.Reset", "/Actions/VirtualMedia.InsertMedia",
                 "/Actions/VirtualMedia.EjectMedia")


def _image_label(url):
    """host + file name of a virtual media URL: its path / query carry the hub's image-service token"""
    from urllib.parse import urlsplit
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.netloc}/…/{parts.path.rstrip('/').rsplit('/', 1)[-1]}"[:200]


def _log_actions(app, path):
    """Every power / virtual media / boot override request a BMC client sent, one JSON line each in `path`
    (read by the app's power history: who reboots a machine shows there)"""
    import flask
    if not path:
        return
    lock = threading.Lock()

    @app.after_request
    def _record(response):
        req = flask.request
        identity = (req.view_args or {}).get("identity")
        if identity is None or req.method not in ("POST", "PATCH") or not req.path.startswith("/redfish/v1/"):
            return response
        body = req.get_json(silent=True) or {}
        if req.method == "POST" and req.path.endswith(_ACTION_PATHS[0]):
            action, detail = f"Redfish reset {body.get('ResetType') or '?'}", None
        elif req.method == "POST" and req.path.endswith(_ACTION_PATHS[1:]):
            action = "virtual media " + ("insert" if req.path.endswith("InsertMedia") else "eject")
            detail = _image_label(str(body.get("Image") or "")) or None
        elif req.method == "PATCH" and "Boot" in body:
            action, detail = "boot override", json.dumps(body.get("Boot"))[:200]
        else:
            return response
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "identity": identity,
                 "action": action, "detail": detail, "client": req.remote_addr, "status": response.status_code}
        with lock:
            try:
                if os.path.exists(path) and os.path.getsize(path) > 1024 * 1024:  # keep the newest half
                    with open(path) as f:
                        lines = f.readlines()
                    with open(path, "w") as f:
                        f.writelines(lines[len(lines) // 2:])
                with open(path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                pass
        return response


def _guard(app, allowed_ids):
    """sushy-tools checks instance_denied on /Systems/<identity> routes only: its VirtualMedia blueprint
    (/Systems/<identity>/VirtualMedia/..., InsertMedia redefines the domain) and /Managers/<identity> (id = the
    domain UUID) have no check, so any VM of the host could be reached with the group's BMC credentials. Deny
    every route that names a VM outside the allow list, and drop them from the Managers collection."""
    import flask
    from sushy_tools import error as sushy_error

    @app.before_request
    def _deny_other_instances():
        identity = (flask.request.view_args or {}).get("identity")
        path = flask.request.path
        if identity is not None and path.startswith(("/redfish/v1/Systems/", "/redfish/v1/Managers/")) \
                and identity not in allowed_ids():
            raise sushy_error.NotFound()

    @app.after_request
    def _filter_managers(response):
        if flask.request.path.rstrip("/") == "/redfish/v1/Managers" and response.status_code == 200:
            try:
                data = json.loads(response.get_data(as_text=True))
                ids = allowed_ids()
                data["Members"] = [m for m in data.get("Members") or []
                                   if m.get("@odata.id", "").rstrip("/").rsplit("/", 1)[-1] in ids]
                data["Members@odata.count"] = len(data["Members"])
                response.set_data(json.dumps(data))
            except (ValueError, AttributeError):
                pass
        return response


def build_app(config):
    """The configured sushy-tools Flask app (not running), restricted to the allow list"""
    _patch_resolver(config.get("hosts") or {}, config.get("redirects") or {}, config.get("redirect_ports") or [])
    from sushy_tools.emulator import api_utils
    from sushy_tools.emulator import main as emulator

    allowed = _Allowed(config["allow_file"], config.get("libvirt_uri") or "qemu:///system")

    def instance_denied(**kwargs):
        identity = kwargs.get("identity")
        deny = identity not in allowed.get()
        if deny:
            api_utils.warning("Instance %s access denied", identity)
        return deny
    api_utils.instance_denied = instance_denied
    # api_utils' logging helper `error()` shadows the error module its access check raises error.NotFound from
    # (a denied system gave a 500): make that lookup work (404)
    from sushy_tools import error as sushy_error
    if not hasattr(api_utils.error, "NotFound"):
        api_utils.error.NotFound = sushy_error.NotFound

    app = emulator.app
    _guard(app, allowed.get)
    _log_actions(app, config.get("actions_file"))
    app.config.update({
        "SUSHY_EMULATOR_LISTEN_IP": config["listen"],
        "SUSHY_EMULATOR_LISTEN_PORT": int(config["port"]),
        "SUSHY_EMULATOR_LIBVIRT_URI": config.get("libvirt_uri") or "qemu:///system",
        "SUSHY_EMULATOR_AUTH_FILE": config["auth_file"],
        "SUSHY_EMULATOR_STATE_DIR": config["state_dir"],
        "SUSHY_EMULATOR_VMEDIA_VERIFY_SSL": False,   # hub routes are signed by the cluster's own CA
        "SUSHY_EMULATOR_IGNORE_BOOT_DEVICE": False,
        "SUSHY_EMULATOR_STORAGE_POOL": config.get("storage_pool") or "default",
    })
    return app, emulator


def main():
    config = _load(sys.argv[1])
    _, emulator = build_app(config)
    argv = ["sushy-emulator", "--interface", config["listen"], "--port", str(config["port"]),
            "--libvirt-uri", config.get("libvirt_uri") or "qemu:///system"]
    if config.get("cert") and config.get("key"):
        argv += ["--ssl-certificate", config["cert"], "--ssl-key", config["key"]]
    sys.argv = argv
    return emulator.main()


if __name__ == "__main__":
    sys.exit(main())
