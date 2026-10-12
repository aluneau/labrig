"""BMC emulator process of one lab group: sushy-tools (Redfish, libvirt driver) restricted to the group's BMC members.

Run by bmc_service as `python -m app.services.bmc_emulator <config.json>` (one process per group, own port).
Only stdlib + sushy_tools + libvirt here: no app settings / DB import.

config.json: {listen, port, libvirt_uri, auth_file (htpasswd, bcrypt), allow_file (JSON list of VM names),
state_dir, hosts: {"<zone>": "<ip>"}, cert?, key?}
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


def _patch_resolver(hosts):
    """getaddrinfo for names under the configured zones -> the zone's address"""
    if not hosts:
        return
    real = socket.getaddrinfo
    zones = sorted(((z.lower().rstrip("."), ip) for z, ip in hosts.items()), key=lambda x: -len(x[0]))

    def getaddrinfo(host, port, *args, **kwargs):
        if isinstance(host, str):
            name = host.lower().rstrip(".")
            for zone, ip in zones:
                if name == zone or name.endswith("." + zone):
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


def main():
    config = _load(sys.argv[1])
    _patch_resolver(config.get("hosts") or {})
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
    argv = ["sushy-emulator", "--interface", config["listen"], "--port", str(config["port"]),
            "--libvirt-uri", config.get("libvirt_uri") or "qemu:///system"]
    if config.get("cert") and config.get("key"):
        argv += ["--ssl-certificate", config["cert"], "--ssl-key", config["key"]]
    sys.argv = argv
    return emulator.main()


if __name__ == "__main__":
    sys.exit(main())
