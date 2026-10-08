"""Check a Linux account's password through PAM (ctypes on libpam: no pip dependency).

The conversation answers the password to "echo off" prompts and the user name to "echo on" ones,
then runs pam_authenticate + pam_acct_mgmt (expired / locked accounts are refused).

Limitation (pam_unix): a non-root process can only verify its OWN password (unix_chkpwd refuses
other users). SSSD / LDAP / Kerberos users (pam_sss, pam_krb5) work from any process. For other
local users the app goes through the root helper's `pam-auth` command (auth_service), which
always uses the fixed service `vm-manager` (never su / sudo: their pam_rootok would let root in).
scripts/vm-manager-helper carries a copy of this code (it can't import the app).
"""
import ctypes
import ctypes.util
from ctypes import CFUNCTYPE, POINTER, Structure, byref, c_char_p, c_int, c_size_t, c_void_p, cast, sizeof
from typing import Optional, Tuple

PAM_PROMPT_ECHO_OFF = 1
PAM_PROMPT_ECHO_ON = 2
PAM_SUCCESS = 0
PAM_BUF_ERR = 5
PAM_CONV_ERR = 19
PAM_DISALLOW_NULL_AUTHTOK = 0x0001


class PamMessage(Structure):
    _fields_ = [("msg_style", c_int), ("msg", c_char_p)]


class PamResponse(Structure):
    _fields_ = [("resp", c_void_p), ("resp_retcode", c_int)]


CONV_FUNC = CFUNCTYPE(c_int, c_int, POINTER(POINTER(PamMessage)), POINTER(POINTER(PamResponse)), c_void_p)


class PamConv(Structure):
    _fields_ = [("conv", CONV_FUNC), ("appdata_ptr", c_void_p)]


_lib = None


def _libs():
    """(libpam, libc), loaded once. Raises OSError when libpam isn't installed."""
    global _lib
    if _lib is None:
        pam_name = ctypes.util.find_library("pam") or "libpam.so.0"
        libpam = ctypes.CDLL(pam_name)
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        libc.calloc.restype = c_void_p
        libc.calloc.argtypes = [c_size_t, c_size_t]
        libc.strdup.restype = c_void_p
        libc.strdup.argtypes = [c_char_p]
        libpam.pam_start.restype = c_int
        libpam.pam_start.argtypes = [c_char_p, c_char_p, POINTER(PamConv), POINTER(c_void_p)]
        if hasattr(libpam, "pam_start_confdir"):  # Linux-PAM >= 1.4 (tests: a stack in a private directory)
            libpam.pam_start_confdir.restype = c_int
            libpam.pam_start_confdir.argtypes = [c_char_p, c_char_p, POINTER(PamConv), c_char_p, POINTER(c_void_p)]
        for name in ("pam_authenticate", "pam_acct_mgmt"):
            getattr(libpam, name).restype = c_int
            getattr(libpam, name).argtypes = [c_void_p, c_int]
        libpam.pam_end.restype = c_int
        libpam.pam_end.argtypes = [c_void_p, c_int]
        libpam.pam_strerror.restype = c_char_p
        libpam.pam_strerror.argtypes = [c_void_p, c_int]
        _lib = (libpam, libc)
    return _lib


def available() -> bool:
    try:
        _libs()
        return True
    except OSError:
        return False


def authenticate(username: str, password: str, service: str = "vm-manager",
                 confdir: Optional[str] = None) -> Tuple[bool, str]:
    """(True, "") when PAM accepts the password and the account; else (False, PAM's reason)."""
    libpam, libc = _libs()
    user_b = username.encode()
    password_b = password.encode()

    @CONV_FUNC
    def conv(n_messages, messages, p_response, _appdata):
        # PAM frees the answers with free(): allocate them with the C library
        addr = libc.calloc(n_messages, sizeof(PamResponse))
        if not addr:
            return PAM_BUF_ERR
        responses = cast(addr, POINTER(PamResponse))
        for i in range(n_messages):
            style = messages[i].contents.msg_style
            if style == PAM_PROMPT_ECHO_OFF:
                responses[i].resp = libc.strdup(password_b)
            elif style == PAM_PROMPT_ECHO_ON:
                responses[i].resp = libc.strdup(user_b)
        p_response[0] = responses
        return PAM_SUCCESS

    handle = c_void_p()
    pam_conv = PamConv(conv, None)
    if confdir:
        if not hasattr(libpam, "pam_start_confdir"):
            return False, "this libpam has no pam_start_confdir (AUTH_PAM_CONFDIR needs Linux-PAM >= 1.4)"
        rc = libpam.pam_start_confdir(service.encode(), user_b, byref(pam_conv), confdir.encode(), byref(handle))
    else:
        rc = libpam.pam_start(service.encode(), user_b, byref(pam_conv), byref(handle))
    if rc != PAM_SUCCESS:
        return False, f"pam_start failed ({rc})"
    try:
        rc = libpam.pam_authenticate(handle, PAM_DISALLOW_NULL_AUTHTOK)
        if rc == PAM_SUCCESS:
            rc = libpam.pam_acct_mgmt(handle, PAM_DISALLOW_NULL_AUTHTOK)
        reason = "" if rc == PAM_SUCCESS else (libpam.pam_strerror(handle, rc) or b"").decode(errors="replace")
        return rc == PAM_SUCCESS, reason
    finally:
        libpam.pam_end(handle, rc)
