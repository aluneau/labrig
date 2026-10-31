"""Redfish client for REAL, physical bare-metal hosts through their BMC: power, one-time boot, virtual
media, inventory.

NOTE: this is for real hardware. It is unrelated to `bmc_service.py` / `bmc_emulator.py`, which run
*emulated* BMCs (sushy-tools) so an ACM hub can drive lab-group VMs like servers. Real hardware =
this file + `baremetal_service.py` (the BareMetalHost resource); emulated = `bmc_service.py`.

Generic Redfish (DMTF) so it works on Dell iDRAC 9+, HPE iLO 5+, Supermicro X11/X12+ and others:
everything is discovered from the service root (`/redfish/v1`), never hardcoded to a vendor path.
BMCs ship self-signed certs, so TLS is not verified by default (a lab on a trusted network); pass
`verify=True` for a BMC with a real certificate.

stdlib only (urllib), like openshift_service: this runs on the host, not in a guest, and must import on
Python 3.9 (RHEL 9). Credentials are passed in by the caller (baremetal_service stores them 0600, never in
the API), never logged here.

Used by baremetal_service (the BareMetalHost resource) and by the OpenShift installer's bare-metal node
path (insert the agent ISO as virtual media, boot it once, power on; eject + boot from disk when installed).
"""
import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

HTTP_TIMEOUT = 30
# Reset types we map to, in order of preference, when the BMC doesn't advertise the obvious one.
_POWER_ON = ["On"]
_POWER_OFF = ["ForceOff", "GracefulShutdown"]
_POWER_CYCLE = ["ForceRestart", "PowerCycle", "GracefulRestart"]


class BMCError(Exception):
    """A Redfish call failed (unreachable, auth, or an action the BMC refused)."""


class RedfishBMC:
    """A single BMC. Discovers the ComputerSystem, Manager and virtual-media collection lazily on
    first use and caches their paths. Methods raise BMCError with a plain-words message."""

    def __init__(self, address: str, username: str, password: str, verify: bool = False,
                 timeout: int = HTTP_TIMEOUT):
        # Accept "10.0.0.1", "https://10.0.0.1", "https://10.0.0.1:443"
        if "://" not in address:
            address = "https://" + address
        parsed = urlparse(address)
        if parsed.scheme not in ("http", "https"):
            raise BMCError(f"BMC address must be http(s): {address!r}")
        self.base = f"{parsed.scheme}://{parsed.netloc}"
        self._user = username
        self._password = password
        self._timeout = timeout
        self._ctx: Optional[ssl.SSLContext] = None
        if parsed.scheme == "https" and not verify:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE
        # discovered lazily
        self._system_path: Optional[str] = None
        self._manager_path: Optional[str] = None
        self._vmedia_path: Optional[str] = None

    # ---------------------------------------------------------------- transport

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
                 allow_codes: tuple = ()) -> Dict[str, Any]:
        url = urljoin(self.base + "/", path.lstrip("/"))
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "OData-Version": "4.0", "User-Agent": "vm-manager"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        # Basic auth: the broadest Redfish auth, no session juggling (fine for short host-side calls).
        pw = f"{self._user}:{self._password}".encode()
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(pw).decode())
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in allow_codes:
                return {}
            detail = self._error_detail(e)
            if e.code in (401, 403):
                raise BMCError(f"BMC rejected the credentials ({e.code})") from e
            raise BMCError(f"BMC {method} {path} failed: {e.code} {detail}") from e
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            raise BMCError(f"BMC unreachable at {self.base}: {getattr(e, 'reason', e)}") from e
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    @staticmethod
    def _error_detail(e: "urllib.error.HTTPError") -> str:
        try:
            info = json.loads(e.read())
            err = info.get("error", {})
            msgs = err.get("@Message.ExtendedInfo") or []
            if msgs:
                return "; ".join(m.get("Message", "") for m in msgs if m.get("Message"))
            return err.get("message", "") or ""
        except Exception:
            return ""

    # ---------------------------------------------------------------- discovery

    def _discover(self) -> None:
        if self._system_path and self._manager_path:
            return
        root = self._request("GET", "/redfish/v1/")
        systems = (root.get("Systems") or {}).get("@odata.id")
        managers = (root.get("Managers") or {}).get("@odata.id")
        if not systems or not managers:
            raise BMCError("not a Redfish BMC (no Systems/Managers in the service root)")
        self._system_path = self._first_member(systems, "ComputerSystem")
        self._manager_path = self._first_member(managers, "Manager")

    def _first_member(self, collection_path: str, what: str) -> str:
        members = self._request("GET", collection_path).get("Members") or []
        if not members:
            raise BMCError(f"BMC exposes no {what}")
        return members[0]["@odata.id"]

    def _system(self) -> str:
        self._discover()
        assert self._system_path
        return self._system_path

    def _vmedia_collection(self) -> str:
        if self._vmedia_path:
            return self._vmedia_path
        self._discover()
        mgr = self._request("GET", self._manager_path)
        vm = (mgr.get("VirtualMedia") or {}).get("@odata.id")
        if not vm:
            raise BMCError("this BMC does not expose VirtualMedia over Redfish")
        self._vmedia_path = vm
        return vm

    # ---------------------------------------------------------------- inventory

    def system_info(self) -> Dict[str, Any]:
        """Vendor/model/serial, power state, CPU/RAM/BIOS — a snapshot for the UI and sizing checks."""
        d = self._request("GET", self._system())
        mem = d.get("MemorySummary") or {}
        cpu = d.get("ProcessorSummary") or {}
        return {
            "manufacturer": d.get("Manufacturer"),
            "model": d.get("Model"),
            "serial": d.get("SerialNumber") or d.get("SKU"),
            "power_state": d.get("PowerState"),
            "health": (d.get("Status") or {}).get("Health"),
            "bios_version": d.get("BiosVersion"),
            "memory_gib": mem.get("TotalSystemMemoryGiB"),
            "cpu_count": cpu.get("Count"),
            "cpu_model": (cpu.get("Model") or "").strip() or None,
            "boot_override": (d.get("Boot") or {}).get("BootSourceOverrideTarget"),
        }

    def nics(self) -> List[Dict[str, Any]]:
        """Physical NICs with MAC / link / speed — the MAC feeds the agent-config host match."""
        d = self._request("GET", self._system())
        coll = (d.get("EthernetInterfaces") or {}).get("@odata.id")
        if not coll:
            return []
        out: List[Dict[str, Any]] = []
        for m in self._request("GET", coll).get("Members") or []:
            iface = self._request("GET", m["@odata.id"])
            mac = iface.get("MACAddress") or iface.get("PermanentMACAddress")
            if not mac:
                continue
            out.append({
                "id": iface.get("Id"),
                "mac": mac.upper(),
                "link": iface.get("LinkStatus"),
                "speed_mbps": iface.get("SpeedMbps"),
                "ipv4": [a.get("Address") for a in iface.get("IPv4Addresses") or [] if a.get("Address")],
            })
        return out

    def screenshot(self) -> bytes:
        """A PNG of the host's video console (a quick "what's on screen" without the KVM applet).
        Dell iDRAC only, through its OEM action; other BMCs raise BMCError."""
        import base64
        self._discover()
        action = f"{self._manager_path}/Oem/Dell/DellLCService/Actions/DellLCService.ExportServerScreenShot"
        try:
            data = self._request("POST", action, {"FileType": "ServerScreenShot"})
        except BMCError:
            raise BMCError("console screenshot is only supported on Dell iDRAC for now")
        b64 = data.get("ServerScreenShotFile") or data.get("ScreenShotData")
        if not b64:
            raise BMCError("console screenshot is only supported on Dell iDRAC for now")
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError) as e:
            raise BMCError(f"bad screenshot data from the BMC: {e}")

    # ---------------------------------------------------------------- power

    def power_state(self) -> Optional[str]:
        return self._request("GET", self._system()).get("PowerState")

    def _reset(self, candidates: List[str]) -> None:
        d = self._request("GET", self._system())
        action = (d.get("Actions") or {}).get("#ComputerSystem.Reset") or {}
        target = action.get("target")
        if not target:
            raise BMCError("BMC exposes no ComputerSystem.Reset action")
        allowed = action.get("ResetType@Redfish.AllowableValues") or candidates
        reset_type = next((c for c in candidates if c in allowed), None)
        if reset_type is None:
            raise BMCError(f"BMC supports none of {candidates} (has {allowed})")
        self._request("POST", target, {"ResetType": reset_type})

    def power_on(self) -> None:
        if (self.power_state() or "").lower() == "on":
            return
        self._reset(_POWER_ON)

    def power_off(self, graceful: bool = False) -> None:
        if (self.power_state() or "").lower() == "off":
            return
        self._reset(["GracefulShutdown", "ForceOff"] if graceful else _POWER_OFF)

    def power_cycle(self) -> None:
        """Reboot if on, else power on — used to kick the host into the freshly inserted media."""
        if (self.power_state() or "").lower() == "on":
            self._reset(_POWER_CYCLE)
        else:
            self._reset(_POWER_ON)

    def wait_power(self, state: str, timeout: int = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (self.power_state() or "").lower() == state.lower():
                return
            time.sleep(3)
        raise BMCError(f"host did not reach power state {state!r} within {timeout}s")

    def wait_power_stable(self, state: str, settle_seconds: int = 75) -> None:
        """Require a reported power state to remain stable before a conflicting action.

        Some iDRAC releases acknowledge ForceOff and briefly report Off before
        the requested transition has reached the host.  Starting a new virtual
        media boot in that window lets the old shutdown turn off the new boot.
        """
        self.wait_power(state)
        deadline = time.monotonic() + settle_seconds
        while time.monotonic() < deadline:
            if (self.power_state() or "").lower() != state.lower():
                self.wait_power(state)
            time.sleep(3)

    def verify_power_on(self, settle_seconds: int = 45) -> None:
        """Fail a handoff when the server falls back off during POST.

        A successful Redfish PowerOn response only says that iDRAC accepted the
        request.  It does not mean the machine made it through firmware POST.
        """
        deadline = time.monotonic() + settle_seconds
        while time.monotonic() < deadline:
            if (self.power_state() or "").lower() != "on":
                raise BMCError("host powered off during firmware boot after virtual-media handoff")
            time.sleep(3)

    # ---------------------------------------------------------------- boot override

    def set_boot_once(self, target: str = "Cd") -> None:
        """One-time boot to `target` (Cd, Pxe, Hdd...), applied on the next power action and cleared by
        the BMC afterwards. Mode follows the current firmware mode (UEFI on modern servers)."""
        d = self._request("GET", self._system())
        boot = d.get("Boot") or {}
        allowed = boot.get("BootSourceOverrideTarget@Redfish.AllowableValues")
        if allowed and target not in allowed:
            raise BMCError(f"BMC cannot boot-override to {target!r} (has {allowed})")
        payload: Dict[str, Any] = {
            "Boot": {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideTarget": target}
        }
        # Do not PATCH BootSourceOverrideMode back verbatim.  Dell iDRAC 6
        # treats that as a persistent BIOS change and creates a ConfigBIOS
        # job, whereas a one-time boot target must remain runtime-only.
        self._request("PATCH", self._system(), payload)

    def clear_boot_override(self) -> None:
        self._request("PATCH", self._system(), {"Boot": {"BootSourceOverrideEnabled": "Disabled"}})

    # ---------------------------------------------------------------- virtual media

    def _cd_device(self) -> Dict[str, Any]:
        """The virtual CD/DVD device (prefer one advertising CD/DVD media)."""
        coll = self._vmedia_collection()
        members = self._request("GET", coll).get("Members") or []
        cd = None
        for m in members:
            dev = self._request("GET", m["@odata.id"])
            types = dev.get("MediaTypes") or []
            if "CD" in types or "DVD" in types or (dev.get("Id") or "").upper() in ("CD", "CD1", "VIRTUALCD"):
                cd = dev
                break
        if cd is None and members:
            cd = self._request("GET", members[0]["@odata.id"])
        if cd is None:
            raise BMCError("no virtual CD/DVD device on this BMC")
        return cd

    def virtual_media_status(self) -> Dict[str, Any]:
        cd = self._cd_device()
        return {"inserted": bool(cd.get("Inserted")), "image": cd.get("Image"),
                "connected_via": cd.get("ConnectedVia")}

    @staticmethod
    def _action_target(dev: Dict[str, Any], name: str) -> Optional[str]:
        return ((dev.get("Actions") or {}).get(f"#VirtualMedia.{name}") or {}).get("target")

    def insert_media(self, image_url: str) -> None:
        """Mount an ISO served over HTTP(S). The BMC fetches `image_url` itself, so it must be reachable
        from the BMC's network (the app serves the agent ISO on an unauthenticated tokenised URL)."""
        if urlparse(image_url).scheme not in ("http", "https"):
            raise BMCError(f"virtual media URL must be http(s): {image_url!r}")
        cd = self._cd_device()
        if cd.get("Inserted"):
            self._eject(cd)
            cd = self._cd_device()
        target = self._action_target(cd, "InsertMedia")
        if not target:
            raise BMCError("BMC virtual CD has no InsertMedia action")
        self._request("POST", target, {"Image": image_url, "Inserted": True, "WriteProtected": True})

    def eject_media(self) -> None:
        self._eject(self._cd_device())

    def _eject(self, cd: Dict[str, Any]) -> None:
        if not cd.get("Inserted"):
            return
        target = self._action_target(cd, "EjectMedia")
        if not target:
            raise BMCError("BMC virtual CD has no EjectMedia action")
        # Some BMCs want an empty body; 400 on an already-ejected device is fine.
        self._request("POST", target, {}, allow_codes=(400,))

    # ---------------------------------------------------------------- high level

    def boot_iso_once(self, image_url: str) -> None:
        """Insert the ISO, set a one-time CD boot and (re)start the host into it — the whole provisioning
        kick in one call."""
        self.insert_media(image_url)
        self.set_boot_once("Cd")
        # On Dell iDRAC, ForceOff can be acknowledged before the controller
        # actually applies it.  A later PowerOn then races that stale request
        # and the freshly booted node turns itself off.  Use the controller's
        # atomic cycle when it is already on; it was the established SNO path.
        if (self.power_state() or "").lower() == "on":
            self.power_cycle()
        else:
            self.power_on()
        self.wait_power("On")

    def erase_disks(self) -> None:
        """Wipe the host's physical disks (decommission). Dell iDRAC only, via the OEM SystemErase action
        (cryptographic erase for self-encrypting drives, overwrite otherwise); other BMCs raise BMCError.
        The host reboots into the Lifecycle Controller to run it — slow, and it powers off afterwards."""
        self._discover()
        base = f"{self._manager_path}/Oem/Dell/DellLCService"
        try:
            svc = self._request("GET", base)
        except BMCError:
            raise BMCError("disk erase is only supported on Dell iDRAC for now")
        action = (svc.get("Actions") or {}).get("#DellLCService.SystemErase") or {}
        target = action.get("target") or f"{base}/Actions/DellLCService.SystemErase"
        allowed = action.get("Component@Redfish.AllowableValues") or []
        # Prefer a cryptographic erase of the physical disks, else overwrite them.
        comp = next((c for c in ("CryptographicErasePD", "OverwritePD") if not allowed or c in allowed), None)
        if comp is None:
            raise BMCError(f"this BMC's SystemErase can't erase disks (components: {allowed})")
        self._request("POST", target, {"Component": [comp]})

    # ---------------------------------------------------------------- DNS (resolver the BMC itself uses)

    def get_dns_servers(self) -> List[str]:
        """The nameservers the BMC uses for its own lookups (for virtual media served at a DNS name).
        Dell iDRAC via its Manager attributes (IPv4.1.DNS1/DNS2); other BMCs via the DMTF
        EthernetInterface.StaticNameServers if present, else []."""
        self._discover()
        attrs = self._dell_attributes()
        if attrs is not None:
            return [attrs[k] for k in ("IPv4.1.DNS1", "IPv4.1.DNS2")
                    if attrs.get(k) and attrs[k] not in ("0.0.0.0", "")]
        # DMTF fallback: the manager's dedicated iDRAC-style NIC
        for iface in self._manager_ethernet_ifaces():
            d = self._request("GET", iface)
            servers = [s for s in (d.get("StaticNameServers") or d.get("NameServers") or []) if s]
            if servers:
                return servers
        return []

    def set_dns_servers(self, servers: List[str]) -> None:
        """Set the BMC's own nameservers (so it can resolve the image-service route to fetch virtual media).
        Dell iDRAC only; other BMCs raise BMCError (the caller treats it as best effort)."""
        self._discover()
        servers = [s for s in servers if s][:2] or ["0.0.0.0"]
        if self._dell_attributes() is None:
            raise BMCError("setting the BMC's DNS is only supported on Dell iDRAC for now")
        body = {"IPv4.1.DNSFromDHCP": "Disabled", "IPv4.1.DNS1": servers[0],
                "IPv4.1.DNS2": servers[1] if len(servers) > 1 else "0.0.0.0"}
        self._request("PATCH", f"{self._manager_path}/Attributes", {"Attributes": body})

    def _dell_attributes(self) -> Optional[Dict[str, Any]]:
        """The Dell iDRAC Manager attributes bag, or None on a non-Dell BMC."""
        try:
            data = self._request("GET", f"{self._manager_path}/Attributes")
        except BMCError:
            return None
        attrs = data.get("Attributes")
        return attrs if isinstance(attrs, dict) else None

    def _manager_ethernet_ifaces(self) -> List[str]:
        mgr = self._request("GET", self._manager_path)
        coll = (mgr.get("EthernetInterfaces") or {}).get("@odata.id")
        if not coll:
            return []
        return [m["@odata.id"] for m in self._request("GET", coll).get("Members") or []]

    def check(self) -> Dict[str, Any]:
        """Reachability + credential probe for the UI: returns system_info, raises BMCError otherwise."""
        return self.system_info()
