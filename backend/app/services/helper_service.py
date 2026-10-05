"""Run the root-owned privileged helper (scripts/vm-manager-helper, installed by setup.sh).

The app never gets sudo: the helper has a fixed command whitelist and validates its arguments
itself; pkexec + the polkit action org.vmmanager.helper let the `libvirt` group run it.
"""
import logging
import os
import shutil
import subprocess
from typing import List

from app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SETUP_HINT = "re-run scripts/setup.sh to install it"

# helper exit codes -> HTTP status
_STATUS = {2: 400, 3: 501, 4: 404, 5: 500}


class HelperError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def run_helper(args: List[str], timeout: int = 30) -> str:
    """Run `helper <args>` as root and return its stdout"""
    if not os.path.isfile(settings.HELPER_PATH):
        raise HelperError(501, f"The privileged helper {settings.HELPER_PATH} is not installed: {SETUP_HINT}")
    if os.geteuid() == 0:
        cmd = [settings.HELPER_PATH, *args]
    else:
        pkexec = shutil.which("pkexec", path=SYSTEM_PATH)
        if pkexec is None:
            raise HelperError(501, f"pkexec is not installed (polkit): {SETUP_HINT}")
        cmd = [pkexec, "--disable-internal-agent", settings.HELPER_PATH, *args]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise HelperError(504, f"The privileged helper timed out ({' '.join(args[:1])})")
    if result.returncode == 0:
        return result.stdout
    err = (result.stderr.strip() or result.stdout.strip()).splitlines()
    message = err[-1] if err else f"exit code {result.returncode}"
    if message.startswith("error: "):
        message = message[len("error: "):]
    logger.warning(f"helper {' '.join(args)} failed ({result.returncode}): {message}")
    if result.returncode in (126, 127):  # pkexec: not authorized / authentication failed
        raise HelperError(403, f"Not allowed to run the privileged helper ({message}). The polkit rule for "
                               f"org.vmmanager.helper may be missing: {SETUP_HINT}. The app's user must be in "
                               f"group 'libvirt'.")
    raise HelperError(_STATUS.get(result.returncode, 500), message)
