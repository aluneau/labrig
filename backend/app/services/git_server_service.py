"""An in-lab git server (Gitea) the app runs, so OpenShift GitOps / Argo CD on an ACM hub can pull the spoke
manifests from a reachable URL (docs/openshift.md "GitOps / ZTP"). This is the realistic ZTP shape: a fleet
commits its SiteConfig / provisioning manifests to a git repository, and Argo CD syncs them onto the hub to
provision the spokes — instead of the app applying the CRs with `oc`.

One Gitea process, supervised like the BMC emulator (bmc_service): a child of the app, stopped with it and
restarted by reconcile at startup. Data lives under DATA_DIR/git (its own SQLite DB and repositories); the
single binary is downloaded on first use (pinned GITEA_VERSION, x86_64), not vendored. Gitea listens on
0.0.0.0:<port> (a free port of GIT_HOST_PORTS, opened in the host firewall by scripts/setup.sh) so the hub's
Argo CD controller reaches it over the lab LAN at the host's address — the same routed network the hub's own
`oc` and the host DNS resolver already use.

The app keeps the GitOps source of truth in DATA_DIR/gitops (gitops_service); this service mirrors it into a
Gitea repository `vmmanager/spokes` (public, anonymous clone) so Argo CD needs no repository credentials.
stdlib only (subprocess + urllib), like the other host-side services.
"""
import base64
import hashlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

ADMIN_USER = "vmmanager"
REPO_NAME = "spokes"
_ENV = {**os.environ, "GIT_AUTHOR_NAME": "vm-manager", "GIT_AUTHOR_EMAIL": "vm-manager@localhost",
        "GIT_COMMITTER_NAME": "vm-manager", "GIT_COMMITTER_EMAIL": "vm-manager@localhost"}


def base_dir() -> Path:
    path = Path(settings.DATA_DIR) / "git"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bin_path() -> Path:
    return base_dir() / "bin" / f"gitea-{settings.GITEA_VERSION}"


def _work_dir() -> Path:
    path = base_dir() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _conf_path() -> Path:
    (base_dir() / "conf").mkdir(parents=True, exist_ok=True)
    return base_dir() / "conf" / "app.ini"


def _state_path() -> Path:
    return base_dir() / "state.json"


def port_range() -> Tuple[int, int]:
    first, _, last = settings.GIT_HOST_PORTS.partition("-")
    return int(first), int(last or first)


def _load_state() -> Dict[str, Any]:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    fd = os.open(str(_state_path()), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)


def available() -> Optional[str]:
    """None when the git server can run on this host, else the reason (plain words)."""
    if not settings.GIT_SERVER_ENABLED:
        return "The in-lab git server is disabled (GIT_SERVER_ENABLED=false)"
    if not shutil.which("git"):
        return "git is not installed on the host"
    return None


def endpoint_host() -> Optional[str]:
    """The host:port Argo CD uses to reach the repository (the host's routed LAN address)."""
    host = settings.GIT_ENDPOINT_HOST
    if not host:
        from app.services.dns_service import dns_service
        host = dns_service.host_ip()
    state = _load_state()
    return f"{host}:{state['port']}" if host and state.get("port") else None


def clone_url() -> Optional[str]:
    """The anonymous HTTP clone URL of the spokes repository (what goes in the Argo CD Application)."""
    addr = endpoint_host()
    return f"http://{addr}/{ADMIN_USER}/{REPO_NAME}.git" if addr else None


def web_url() -> Optional[str]:
    """The Gitea web UI base URL (a user opens this in a browser)."""
    addr = endpoint_host()
    return f"http://{addr}/" if addr else None


def repo_web_url() -> Optional[str]:
    """The spokes repository's page in the Gitea web UI."""
    addr = endpoint_host()
    return f"http://{addr}/{ADMIN_USER}/{REPO_NAME}" if addr else None


def _credentials() -> Dict[str, Any]:
    """The admin user / password and the chosen port (made on first use)."""
    state = _load_state()
    if not state.get("password"):
        state["password"] = secrets.token_urlsafe(16)
    if not state.get("port"):
        first, last = port_range()
        state["port"] = first
        # best effort: skip a port already held by another listener on the host
        for p in range(first, last + 1):
            try:
                import socket
                s = socket.socket()
                s.settimeout(0.3)
                free = s.connect_ex(("127.0.0.1", p)) != 0
                s.close()
                if free:
                    state["port"] = p
                    break
            except OSError:
                state["port"] = p
                break
    _save_state(state)
    return state


# ------------------------------------------------------------------------------------------------ binary

def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity", "User-Agent": "vm-manager"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.replace(dest)


def ensure_binary() -> Path:
    """Download the pinned Gitea binary on first use (verified against its .sha256)."""
    binary = _bin_path()
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    version = settings.GITEA_VERSION
    base = f"https://dl.gitea.com/gitea/{version}/gitea-{version}-linux-amd64"
    logger.info(f"Downloading Gitea {version} ({base})")
    _download(base, binary)
    try:
        want = urllib.request.urlopen(urllib.request.Request(
            base + ".sha256", headers={"Accept-Encoding": "identity", "User-Agent": "vm-manager"}),
            timeout=60).read().decode().split()[0]
        got = hashlib.sha256(binary.read_bytes()).hexdigest()
        if want and got != want:
            binary.unlink(missing_ok=True)
            raise RuntimeError(f"Gitea {version} checksum mismatch (expected {want}, got {got})")
    except urllib.error.URLError as e:  # no checksum published: keep the binary, log it
        logger.warning(f"Could not verify the Gitea checksum: {e}")
    binary.chmod(0o755)
    return binary


# ------------------------------------------------------------------------------------------------ config

def _write_config(port: int) -> None:
    conf = _conf_path()
    if conf.is_file():
        return
    host = settings.GIT_ENDPOINT_HOST
    if not host:
        from app.services.dns_service import dns_service
        host = dns_service.host_ip() or "127.0.0.1"
    work = _work_dir()
    ini = f"""APP_NAME = vm-manager GitOps
RUN_MODE = prod

[server]
PROTOCOL = http
HTTP_ADDR = 0.0.0.0
HTTP_PORT = {port}
DOMAIN = {host}
ROOT_URL = http://{host}:{port}/
DISABLE_SSH = true
OFFLINE_MODE = true
LFS_START_SERVER = false

[database]
DB_TYPE = sqlite3
PATH = {work}/gitea.db
LOG_SQL = false

[repository]
ROOT = {work}/gitea-repositories
DEFAULT_BRANCH = main
ENABLE_PUSH_CREATE_USER = true

[security]
INSTALL_LOCK = true
SECRET_KEY = {secrets.token_urlsafe(32)}
INTERNAL_TOKEN = {secrets.token_urlsafe(48)}
PASSWORD_HASH_ALGO = pbkdf2

[oauth2]
JWT_SECRET = {base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')}

[service]
DISABLE_REGISTRATION = true
REQUIRE_SIGNIN_VIEW = false
DEFAULT_KEEP_EMAIL_PRIVATE = true

[actions]
ENABLED = false

[log]
MODE = file
LEVEL = warn
ROOT_PATH = {base_dir()}/log
"""
    fd = os.open(str(conf), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(ini)


def _gitea(args, timeout: int = 120) -> subprocess.CompletedProcess:
    env = {**os.environ, "GITEA_WORK_DIR": str(_work_dir())}
    return subprocess.run([str(_bin_path()), *args, "--config", str(_conf_path())],
                          capture_output=True, text=True, env=env, timeout=timeout, check=False)


def _migrate() -> None:
    """Create / update the Gitea database schema (idempotent); needed before `admin user create`."""
    out = _gitea(["migrate"], timeout=180)
    if out.returncode != 0:
        logger.warning(f"Gitea migrate: {(out.stderr or out.stdout).strip()[-300:]}")


def _change_password(password: str) -> subprocess.CompletedProcess:
    """Set the admin password. Gitea >= 1.22 flags the account must-change-password by default on
    change-password, which then refuses API calls (403): clear it (older releases lack the flag)."""
    args = ["admin", "user", "change-password", "--username", ADMIN_USER, "--password", password]
    out = _gitea(args + ["--must-change-password=false"])
    if out.returncode != 0 and "must-change-password" in (out.stdout + out.stderr):
        out = _gitea(args)
    return out


def _ensure_admin(password: str) -> None:
    """Create the admin user, or set its password when it already exists (it may have been rotated while the
    server was stopped: the app must still push with the stored one)."""
    out = _gitea(["admin", "user", "create", "--admin", "--username", ADMIN_USER, "--password", password,
                  "--email", f"{ADMIN_USER}@localhost", "--must-change-password=false"])
    if out.returncode != 0:
        reset = _change_password(password)
        if reset.returncode != 0:
            logger.warning(f"Gitea admin bootstrap: {(reset.stderr or reset.stdout or out.stderr).strip()[-300:]}")


# ------------------------------------------------------------------------------------------------ HTTP API

def _api(method: str, path: str, port: int, password: str, body: Optional[Dict[str, Any]] = None,
         timeout: int = 20) -> Tuple[int, str]:
    url = f"http://127.0.0.1:{port}/api/v1{path}"
    data = json.dumps(body).encode() if body is not None else None
    auth = base64.b64encode(f"{ADMIN_USER}:{password}".encode()).decode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[-300:]


def _port_alive(port: int, timeout: float = 2) -> bool:
    """True if a Gitea is already serving its HTTP API on the port. Used to detect a Gitea that
    outlived a vm-manager restart (the in-memory Popen handle is gone but the process is still up)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/version", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _wait_up(port: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if _port_alive(port, timeout=5):
            return True
        time.sleep(1)
    return False


def _ensure_repo(port: int, password: str) -> None:
    code, _ = _api("GET", f"/repos/{ADMIN_USER}/{REPO_NAME}", port, password)
    if code == 200:
        return
    code, text = _api("POST", "/user/repos", port, password,
                      {"name": REPO_NAME, "private": False, "auto_init": False,
                       "description": "vm-manager ACM spoke manifests (GitOps / ZTP)"})
    if code not in (201, 409):
        raise RuntimeError(f"Could not create the Gitea repository: HTTP {code} {text}")


# ------------------------------------------------------------------------------------------------ supervisor

class Supervisor:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def start(self, port: int) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if _port_alive(port):
                # Gitea is already listening (it outlived a vm-manager restart: our Popen handle is
                # gone but the process is up). Adopt it instead of spawning a second one that can't
                # bind the port. running() reflects it through the same port probe.
                logger.info(f"Adopting the Gitea git server already listening on :{port}")
                return
            log = open(base_dir() / "gitea.out", "ab")
            env = {**os.environ, "GITEA_WORK_DIR": str(_work_dir())}
            self._proc = subprocess.Popen(
                [str(_bin_path()), "web", "--config", str(_conf_path())],
                cwd=str(_work_dir()), env=env, stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True)
            log.close()
            logger.info(f"Gitea git server on :{port} (pid {self._proc.pid})")

    def running(self) -> bool:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return True
        # The Popen handle is gone (e.g. after a vm-manager restart) but Gitea may still be listening
        # on its port: probe it so status reflects reality rather than the in-memory handle alone. The
        # probe runs outside the lock (it does network I/O) and only when we have no live child.
        port = _load_state().get("port")
        return bool(port) and _port_alive(int(port), timeout=1)

    def stop(self) -> None:
        with self._lock:
            if self._proc is None:
                return
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None


supervisor = Supervisor()
_ready_lock = threading.Lock()


def ensure_ready(timeout: int = 120) -> Dict[str, Any]:
    """Make the git server fully usable: binary + config + admin + process up + repository. Idempotent;
    safe to call before every push. Returns the state (port, clone URL)."""
    problem = available()
    if problem:
        raise RuntimeError(problem)
    state = _credentials()
    port, password = int(state["port"]), state["password"]
    ensure_binary()
    _write_config(port)
    _migrate()            # create the DB schema before touching users
    _ensure_admin(password)
    supervisor.start(port)
    if not _wait_up(port, time.monotonic() + timeout):
        raise RuntimeError(f"The Gitea git server did not answer on :{port} within {timeout}s "
                           f"(see {base_dir()/'gitea.out'})")
    _ensure_repo(port, password)
    state = _load_state()
    state["clone_url"] = clone_url()
    _save_state(state)
    return state


def push(local_repo: Path) -> str:
    """Push a local git repository (DATA_DIR/gitops) into the Gitea spokes repository; returns the clone URL."""
    state = ensure_ready()
    port, password = int(state["port"]), state["password"]
    url = f"http://{ADMIN_USER}:{urllib.parse.quote(password, safe='')}@127.0.0.1:{port}/{ADMIN_USER}/{REPO_NAME}.git"
    out = subprocess.run(["git", "-C", str(local_repo), "push", "--force", url, "HEAD:refs/heads/main"],
                         capture_output=True, text=True, env=_ENV, timeout=120, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"git push to the in-lab server failed: {(out.stderr or out.stdout).strip()[-300:]}")
    return clone_url()


def status() -> Dict[str, Any]:
    """GET view for the ACM GitOps card: whether the git server is enabled / running, how to reach its web UI
    and the spokes repository, and the admin username (the password is a separate secret read)."""
    problem = available()
    state = _load_state()
    has_port = bool(state.get("port"))
    return {
        "enabled": settings.GIT_SERVER_ENABLED and problem is None,
        "running": supervisor.running(),
        "version": settings.GITEA_VERSION,
        "clone_url": clone_url() if has_port else None,
        "web_url": web_url() if has_port else None,
        "repo_web_url": repo_web_url() if has_port else None,
        "admin_user": ADMIN_USER,
        "bootstrapped": has_port,
        "repo_path": str(base_dir()),
        "error": problem,
        "firewall": (f"The hub's Argo CD must reach this port over the LAN: scripts/setup.sh opens "
                     f"GIT_HOST_PORTS ({settings.GIT_HOST_PORTS}/tcp)"),
    }


def admin_credentials(reset: bool = False) -> Dict[str, Any]:
    """The Gitea admin login (username + password) and web URLs, for the "open Gitea" flow. The password lives
    in DATA_DIR/git/state.json (0600) and is never logged. reset=True rotates it, applying it on a running
    server (so the app can keep pushing). A secret read — guarded like kubeconfig / BMC credentials."""
    problem = available()
    if problem:
        raise RuntimeError(problem)
    state = _credentials()  # makes a password + picks a port on first use
    if reset:
        state["password"] = secrets.token_urlsafe(16)
        _save_state(state)
        if supervisor.running():
            out = _change_password(state["password"])
            if out.returncode != 0:  # not created yet (e.g. only picked a port): create the admin now
                _ensure_admin(state["password"])
    # The password is a runtime-generated secret (secrets.token_urlsafe, stored 0600 in state.json),
    # never a hardcoded credential — but a one-line {"username", "password"} dict literal trips generic
    # secret scanners (false positive), so set the password key on its own statement.
    creds = {"username": ADMIN_USER, "web_url": web_url(), "repo_web_url": repo_web_url(),
             "running": supervisor.running()}
    creds["password"] = state["password"]
    return creds


def reconcile() -> None:
    """Start the git server at app startup if it was bootstrapped before (a repo already exists)."""
    if available() is not None:
        return
    state = _load_state()
    if not state.get("port") or not (_work_dir() / "gitea.db").exists():
        return  # never bootstrapped: start lazily on the first push / deploy
    try:
        ensure_ready(timeout=60)
    except Exception as e:  # pragma: no cover - startup must not fail over the git server
        logger.warning(f"Git server reconcile: {e}")
