"""ed25519 SSH key pairs in pure Python (OpenSSH formats), no ssh-keygen.

Why not ssh-keygen: on SELinux hosts (EL) it runs confined (ssh_keygen_t) and may only write keys under home
directories; a release install keeps its data in /var/lib/vm-manager, where it gets "Permission denied".
Ed25519 per RFC 8032 (public key = [clamped SHA-512(seed)[:32]] * B); fast enough for one key per cluster.
"""
import base64
import hashlib
import os
import struct
from pathlib import Path
from typing import Tuple

_P = 2 ** 255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_GY = 4 * pow(5, _P - 2, _P) % _P


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    return _P - x if x % 2 else x


_B = (_xrecover(_GY), _GY, 1, _xrecover(_GY) * _GY % _P)  # extended coordinates (X, Y, Z, T)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _encode(p) -> bytes:
    zi = pow(p[2], _P - 2, _P)
    x, y = p[0] * zi % _P, p[1] * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def public_from_seed(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _encode(_mul(a, _B))


def _string(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def keypair(comment: str = "") -> Tuple[str, str]:
    """-> (private key in OpenSSH PEM format, public key line `ssh-ed25519 AAAA… comment`)"""
    seed = os.urandom(32)
    pub = public_from_seed(seed)
    ktype = b"ssh-ed25519"
    pub_blob = _string(ktype) + _string(pub)
    check = os.urandom(4)
    private = check + check + _string(ktype) + _string(pub) + _string(seed + pub) + _string(comment.encode())
    private += bytes(range(1, 1 + (-len(private) % 8)))  # padding 1, 2, 3… to the cipher block size (8, "none")
    blob = (b"openssh-key-v1\0" + _string(b"none") + _string(b"none") + _string(b"") + struct.pack(">I", 1)
            + _string(pub_blob) + _string(private))
    b64 = base64.b64encode(blob).decode()
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    pem += "\n".join(b64[i:i + 70] for i in range(0, len(b64), 70))
    pem += "\n-----END OPENSSH PRIVATE KEY-----\n"
    line = f"ssh-ed25519 {base64.b64encode(pub_blob).decode()}" + (f" {comment}" if comment else "")
    return pem, line


def write_keypair(key: Path, comment: str = "") -> str:
    """Write `key` (0600) and `key.pub` (0644); returns the public key line"""
    pem, line = keypair(comment)
    fd = os.open(str(key), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pem)
    Path(str(key) + ".pub").write_text(line + "\n")
    return line
