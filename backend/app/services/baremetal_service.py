"""REAL bare-metal hosts (the BareMetalHost resource): CRUD + BMC actions (power, inventory).

This manages *physical* servers driven through their own BMC. Do NOT confuse it with `bmc_service.py` /
`bmc_emulator.py`, which run *emulated* BMCs (sushy-tools) in front of lab-group VMs for an ACM hub.
Real hardware -> this file + `bmc.py`; emulated BMCs -> `bmc_service.py`.

The DB row (models/bmh.BareMetalHost) is the source of truth; the BMC password lives only in a 0600 file
under DATA_DIR/bmc (like the OpenShift pull secret) and is never returned by the API. Redfish calls go
through services/bmc.RedfishBMC. The OpenShift bare-metal node path reuses client()/boot_iso_once here.
"""
import logging
import os
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.bmh import BareMetalHost
from app.schemas.bmh import BareMetalHostCreate, BareMetalHostUpdate
from app.services.bmc import BMCError, RedfishBMC

logger = logging.getLogger(__name__)


class BareMetalService:
    @property
    def _dir(self) -> Path:
        path = Path(settings.DATA_DIR) / "bmc"
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    def _secret_path(self, bmh_id: int) -> Path:
        return self._dir / f"{bmh_id}.secret"

    def _write_secret(self, bmh_id: int, password: str) -> None:
        path = self._secret_path(bmh_id)
        with open(path, "w") as f:
            f.write(password)
        os.chmod(path, 0o600)

    def _read_secret(self, bmh_id: int) -> str:
        try:
            return self._secret_path(bmh_id).read_text()
        except FileNotFoundError:
            raise BMCError("BMC password is missing (re-enter it with an update)")

    # --------------------------------------------------------------- CRUD

    def list_hosts(self, db: Session) -> List[BareMetalHost]:
        return db.query(BareMetalHost).order_by(BareMetalHost.id).all()

    def get_host(self, db: Session, bmh_id: int) -> Optional[BareMetalHost]:
        return db.get(BareMetalHost, bmh_id)

    def create_host(self, db: Session, data: BareMetalHostCreate) -> BareMetalHost:
        if db.query(BareMetalHost).filter(BareMetalHost.name == data.name).first():
            raise ValueError(f"A bare-metal host named '{data.name}' already exists")
        host = BareMetalHost(
            name=data.name, bmc_address=self._normalise(data.bmc_address),
            bmc_username=data.bmc_username, boot_mac=(data.boot_mac or "").upper() or None,
            status="available",
        )
        db.add(host)
        db.commit()
        db.refresh(host)
        self._write_secret(host.id, data.bmc_password)
        # Probe the BMC now so a typo surfaces immediately; failure is non-fatal (status=error).
        try:
            self._refresh(db, host, self.client(db, host))
        except BMCError as e:
            host.status = "error"
            host.status_message = str(e)
            db.commit()
            db.refresh(host)
        return host

    def update_host(self, db: Session, bmh_id: int, data: BareMetalHostUpdate) -> Optional[BareMetalHost]:
        host = self.get_host(db, bmh_id)
        if not host:
            return None
        if data.bmc_address is not None:
            host.bmc_address = self._normalise(data.bmc_address)
        if data.bmc_username is not None:
            host.bmc_username = data.bmc_username
        if data.boot_mac is not None:
            host.boot_mac = data.boot_mac.upper() or None
        if data.bmc_password is not None:
            self._write_secret(host.id, data.bmc_password)
        db.commit()
        db.refresh(host)
        return host

    def delete_host(self, db: Session, bmh_id: int) -> bool:
        host = self.get_host(db, bmh_id)
        if not host:
            return False
        if host.cluster_name:
            raise ValueError(f"'{host.name}' is a node of cluster '{host.cluster_name}'; delete the cluster first")
        # Leave the physical machine in a clean state: eject any media and power it off (best effort — an
        # unreachable BMC must not block removing the host from the app).
        try:
            bmc = self.client(db, host)
            bmc.eject_media()
            bmc.power_off()
        except BMCError as e:
            logger.warning(f"Could not power off {host.name} before delete: {e}")
        db.delete(host)
        db.commit()
        try:
            self._secret_path(bmh_id).unlink()
        except FileNotFoundError:
            pass
        return True

    def reset(self, db: Session, bmh_id: int, wipe: bool = True) -> Optional[BareMetalHost]:
        """Decommission a host: eject media, optionally wipe its disks, and power it off. Refuses while the
        host backs a cluster. `wipe` runs the BMC's disk erase (Dell iDRAC only)."""
        host = self.get_host(db, bmh_id)
        if not host:
            return None
        if host.cluster_name:
            raise ValueError(f"'{host.name}' backs cluster '{host.cluster_name}'; delete the cluster first")
        bmc = self.client(db, host)
        bmc.eject_media()
        bmc.clear_boot_override()
        if wipe:
            bmc.erase_disks()  # Dell: reboots into the LC, wipes, powers off; raises BMCError on others
        else:
            bmc.power_off()
        host.status = "available"
        host.status_message = "Disks erased" if wipe else None
        db.commit()
        db.refresh(host)
        return host

    # --------------------------------------------------------------- BMC actions

    def client(self, db: Session, host: BareMetalHost) -> RedfishBMC:
        return RedfishBMC(host.bmc_address, host.bmc_username, self._read_secret(host.id))

    def credentials(self, host: BareMetalHost) -> dict:
        """The BMC username + password, for a metal3 BareMetalHost secret (never returned by the API)."""
        return {"username": host.bmc_username, "password": self._read_secret(host.id)}

    def redfish_virtualmedia_address(self, db: Session, host: BareMetalHost) -> str:
        """metal3 BareMetalHost `bmc.address` for this host: a redfish-virtualmedia:// URL with the host's
        ComputerSystem path (discovered from the BMC, e.g. /redfish/v1/Systems/System.Embedded.1 on Dell)."""
        from urllib.parse import urlparse
        netloc = urlparse(host.bmc_address).netloc or urlparse("//" + host.bmc_address).netloc
        try:
            system = self.client(db, host)._system()  # discovered ComputerSystem @odata.id
        except BMCError:
            system = "/redfish/v1/Systems/System.Embedded.1"
        return f"redfish-virtualmedia://{netloc}{system}"

    def refresh(self, db: Session, bmh_id: int) -> Optional[BareMetalHost]:
        """Re-read inventory/power from the BMC; records BMCError as status=error."""
        host = self.get_host(db, bmh_id)
        if not host:
            return None
        try:
            self._refresh(db, host, self.client(db, host))
        except BMCError as e:
            host.status = "error"
            host.status_message = str(e)
            db.commit()
            db.refresh(host)
        return host

    def _refresh(self, db: Session, host: BareMetalHost, bmc: RedfishBMC) -> None:
        info = bmc.system_info()
        nics = bmc.nics()
        host.inventory = {**info, "nics": nics}
        if not host.boot_mac:
            up = next((n["mac"] for n in nics if (n.get("link") or "").lower() == "linkup"), None)
            host.boot_mac = up or (nics[0]["mac"] if nics else None)
        # Don't clobber provisioning/provisioned with the generic probe status
        if host.status in ("available", "error"):
            host.status = "available"
            host.status_message = None
        db.commit()
        db.refresh(host)

    def power(self, db: Session, host: BareMetalHost, action: str, graceful: bool = False) -> str:
        bmc = self.client(db, host)
        if action == "on":
            bmc.power_on()
        elif action == "off":
            bmc.power_off(graceful=graceful)
        elif action == "cycle":
            bmc.power_cycle()
        else:
            raise ValueError(f"unknown power action '{action}'")
        return bmc.power_state() or "unknown"

    def nics(self, db: Session, host: BareMetalHost) -> List[dict]:
        return self.client(db, host).nics()

    def screenshot(self, db: Session, host: BareMetalHost) -> bytes:
        return self.client(db, host).screenshot()

    @staticmethod
    def _normalise(address: str) -> str:
        address = address.strip()
        return address if "://" in address else "https://" + address


baremetal_service = BareMetalService()
