"""Authentication (docs/auth.md): Linux accounts through PAM, roles from Linux groups,
server-side browser sessions (cookie) and API tokens (Bearer), login backoff, audit log.

Passwords never touch the DB: PAM checks them (in-process, or through the root helper for local
users other than the app's own account). Roles are resolved from the user's groups on every use
(cached a minute), so removing someone from the group also disables their sessions and tokens.
"""
import grp
import hashlib
import logging
import os
import pwd
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.models.auth import ApiToken, AuthSession
from app.services import pam_auth
from app.services.helper_service import HelperError, run_helper

logger = logging.getLogger(__name__)
audit_log = logging.getLogger("vmm.audit")

SESSION_COOKIE = "vmm_session"
CSRF_HEADER = "x-vmm-request"  # required on writes that don't use a Bearer token
TOKEN_PREFIX = "vmm_"
ROLES = ("admin", "viewer")
HELPER_MIN_VERSION = 4  # scripts/vm-manager-helper with `pam-auth`


@dataclass
class AuthUser:
    name: str
    role: str  # admin | viewer
    via: str  # session | token | disabled
    token_id: Optional[int] = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


ANONYMOUS_ADMIN = AuthUser(name="anonymous", role="admin", via="disabled")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _split(groups: str) -> List[str]:
    return [g.strip() for g in groups.split(",") if g.strip()]


def service_user() -> str:
    """The Linux account the app runs as (always admin)"""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


# ---- roles ------------------------------------------------------------------------------------

_role_cache: Dict[str, Tuple[float, Optional[str]]] = {}
_ROLE_TTL = 60.0


def user_groups(username: str) -> List[str]:
    """Names of the user's groups, primary included, read from NSS (files, SSSD…) now: a group change
    applies without restarting the app or logging in again on the host"""
    pw = pwd.getpwnam(username)
    names = []
    for gid in os.getgrouplist(pw.pw_name, pw.pw_gid):
        try:
            names.append(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return names


def role_of(username: str, cached: bool = True) -> Optional[str]:
    """admin | viewer | None (not allowed in, or no such user)"""
    now = time.monotonic()
    hit = _role_cache.get(username)
    if cached and hit and now - hit[0] < _ROLE_TTL:
        return hit[1]
    role = None
    try:
        if username == service_user():
            role = "admin"
        else:
            groups = set(user_groups(username))
            if pwd.getpwnam(username).pw_uid == 0:
                role = None  # root never logs in to the web UI
            elif groups & set(_split(settings.AUTH_ADMIN_GROUPS)):
                role = "admin"
            elif groups & set(_split(settings.AUTH_VIEWER_GROUPS)):
                role = "viewer"
    except KeyError:
        role = None
    _role_cache[username] = (now, role)
    return role


# ---- PAM --------------------------------------------------------------------------------------

def pam_service() -> str:
    """The configured service if /etc/pam.d has it, else a distro default that has auth + account"""
    service = settings.AUTH_PAM_SERVICE
    if settings.AUTH_PAM_CONFDIR or os.path.isfile(f"/etc/pam.d/{service}"):
        return service
    for fallback in ("system-auth", "login"):  # EL / Arch / Fedora, then Debian (common-auth has no account)
        if os.path.isfile(f"/etc/pam.d/{fallback}"):
            return fallback
    return service  # PAM's "other"


def _helper_usable() -> bool:
    return settings.AUTH_PAM_HELPER and not settings.AUTH_PAM_CONFDIR and os.path.isfile(settings.HELPER_PATH)


def check_password(username: str, password: str) -> Tuple[bool, str]:
    """(ok, reason for the log). Blocking (PAM delays failures ~2 s): call it from a thread."""
    if not password or len(password) > 4096:
        return False, "empty or oversized password"
    if os.getuid() != 0 and username != service_user() and _helper_usable():
        # pam_unix from a non-root process only checks the caller's own password: ask the root helper
        try:
            run_helper(["pam-auth", username], timeout=60, input=password)
            return True, ""
        except HelperError as e:
            if e.status_code == 401:
                return False, e.detail
            logger.warning(f"PAM through the helper failed for {username}: {e.detail} "
                           f"(re-run scripts/setup.sh to update the helper)")
            return False, f"helper: {e.detail}"
    try:
        return pam_auth.authenticate(username, password, pam_service(), settings.AUTH_PAM_CONFDIR or None)
    except OSError as e:
        logger.error(f"libpam is not usable: {e}")
        return False, "libpam unavailable"


# ---- login backoff ----------------------------------------------------------------------------

class LoginLimiter:
    """Exponential backoff per (IP, user) after 3 failures (2 s, 4 s… up to 5 min), and a per-IP
    ceiling (30 failures in 15 min, whatever the user names) against spraying. Memory only."""
    FREE_FAILURES = 3
    MAX_DELAY = 300
    IP_WINDOW = 900
    IP_MAX = 30

    def __init__(self):
        self._lock = threading.Lock()
        self._pairs: Dict[Tuple[str, str], Tuple[int, float]] = {}  # -> (failures, blocked until)
        self._ips: Dict[str, List[float]] = {}

    def retry_after(self, ip: str, username: str) -> int:
        """Seconds to wait before another attempt is allowed (0 = now)"""
        now = time.time()
        with self._lock:
            recent = [t for t in self._ips.get(ip, []) if now - t < self.IP_WINDOW]
            self._ips[ip] = recent
            if len(recent) >= self.IP_MAX:
                return int(recent[0] + self.IP_WINDOW - now) + 1
            _, until = self._pairs.get((ip, username.lower()), (0, 0.0))
            return max(0, int(until - now + 0.999))

    def failed(self, ip: str, username: str) -> None:
        now = time.time()
        with self._lock:
            key = (ip, username.lower())
            count, _ = self._pairs.get(key, (0, 0.0))
            count += 1
            delay = 0 if count < self.FREE_FAILURES else min(self.MAX_DELAY, 2 ** (count - self.FREE_FAILURES + 1))
            self._pairs[key] = (count, now + delay)
            self._ips.setdefault(ip, []).append(now)
            if len(self._pairs) > 10000:  # bound memory
                self._pairs = {k: v for k, v in self._pairs.items() if v[1] > now}

    def succeeded(self, ip: str, username: str) -> None:
        with self._lock:
            self._pairs.pop((ip, username.lower()), None)


limiter = LoginLimiter()


# ---- sessions ---------------------------------------------------------------------------------

def create_session(db: Session, username: str, ip: str, user_agent: str) -> str:
    """New session; returns the cookie value (only its hash is stored)"""
    now = datetime.utcnow()
    db.query(AuthSession).filter(AuthSession.expires_at < now).delete()
    value = secrets.token_urlsafe(32)
    db.add(AuthSession(id=_sha256(value), username=username, created_at=now, last_seen_at=now,
                       expires_at=now + timedelta(hours=settings.AUTH_SESSION_HOURS),
                       ip=ip[:64], user_agent=(user_agent or "")[:255]))
    db.commit()
    return value


def session_user(db: Session, value: str) -> Optional[str]:
    """User name of a live session (sliding expiry, written at most once a minute)"""
    if not value or len(value) > 128:
        return None
    row = db.get(AuthSession, _sha256(value))
    now = datetime.utcnow()
    if row is None:
        return None
    if row.expires_at < now:
        db.delete(row)
        db.commit()
        return None
    if (now - row.last_seen_at).total_seconds() > 60:
        cap = row.created_at + timedelta(days=settings.AUTH_SESSION_MAX_DAYS)
        row.last_seen_at = now
        row.expires_at = min(cap, now + timedelta(hours=settings.AUTH_SESSION_HOURS))
        db.commit()
    return row.username


def delete_session(db: Session, value: str) -> None:
    if value:
        db.query(AuthSession).filter(AuthSession.id == _sha256(value)).delete()
        db.commit()


def delete_user_sessions(db: Session, username: str) -> int:
    count = db.query(AuthSession).filter(AuthSession.username == username).delete()
    db.commit()
    return count


# ---- API tokens -------------------------------------------------------------------------------

def create_token(db: Session, username: str, name: str, expires_days: Optional[float] = None) -> Tuple[ApiToken, str]:
    """New token for `username`; returns (row, plaintext). The plaintext is never stored."""
    value = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = datetime.utcnow()
    row = ApiToken(username=username, name=name, token_hash=_sha256(value), prefix=value[:12], created_at=now,
                   expires_at=now + timedelta(days=expires_days) if expires_days else None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, value


def token_row(db: Session, value: str, ip: str = "") -> Optional[ApiToken]:
    if not value.startswith(TOKEN_PREFIX) or len(value) > 128:
        return None
    row = db.query(ApiToken).filter(ApiToken.token_hash == _sha256(value)).first()
    now = datetime.utcnow()
    if row is None or (row.expires_at is not None and row.expires_at < now):
        return None
    if row.last_used_at is None or (now - row.last_used_at).total_seconds() > 60 or row.last_used_ip != ip:
        row.last_used_at = now
        row.last_used_ip = ip[:64]
        db.commit()
    return row


# ---- startup ----------------------------------------------------------------------------------

def _listen_host() -> str:
    """--host given to uvicorn (setup.sh / run.sh pass it), else uvicorn's default"""
    import sys
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == "--host" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--host="):
            return arg.split("=", 1)[1]
    return "127.0.0.1"


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def log_startup() -> None:
    host = _listen_host()
    if not settings.AUTH_ENABLED:
        if is_loopback(host):
            logger.warning("Authentication is DISABLED (AUTH_ENABLED=false): anyone who can reach the app is admin")
        else:
            logger.warning(f"Authentication is DISABLED (AUTH_ENABLED=false) and the app listens on {host}: "
                           f"ANYONE on the network can create VMs on this host. Set AUTH_ENABLED=true "
                           f"or listen on 127.0.0.1")
        return
    if not pam_auth.available():
        logger.error("libpam not found: nobody can log in (API tokens still work). Install pam / libpam.")
    service = pam_service()
    if service != settings.AUTH_PAM_SERVICE:
        logger.warning(f"/etc/pam.d/{settings.AUTH_PAM_SERVICE} is missing: using PAM service '{service}' "
                       f"(re-run scripts/setup.sh to install it)")
    missing = [g for g in _split(settings.AUTH_ADMIN_GROUPS) + _split(settings.AUTH_VIEWER_GROUPS)
               if not _group_exists(g)]
    helper = "helper for other local users" if _helper_usable() else "in-process only"
    if _helper_usable() and not settings.AUTH_PAM_CONFDIR:
        from app.services.sriov_service import sriov_service
        version = sriov_service.helper_version() or 0
        if version < HELPER_MIN_VERSION:
            logger.warning(f"The privileged helper is version {version}: logins of local users other than "
                           f"{service_user()} need version {HELPER_MIN_VERSION} (pam-auth). Re-run scripts/setup.sh")
    logger.info(f"Authentication: Linux accounts via PAM service '{service}' ({helper}); admin = {service_user()} "
                f"+ groups [{settings.AUTH_ADMIN_GROUPS}], viewer = groups [{settings.AUTH_VIEWER_GROUPS}]"
                + (f"; groups not found on this host: {', '.join(missing)}" if missing else ""))


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False
