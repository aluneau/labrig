"""ASGI middleware guarding /api: every HTTP request and WebSocket (VNC bridge) is authenticated
by a session cookie or a Bearer token, except a few public paths. Also enforces the viewer role
(read-only), CSRF rules for cookie-authenticated writes and writes the audit log.

Pure ASGI (not BaseHTTPMiddleware): it must see WebSockets and must not buffer the SSE stream.
"""
import json
import logging
import re
from http.cookies import SimpleCookie
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

from anyio import to_thread

from app.config import settings
from app.database import SessionLocal
from app.services import auth_service
from app.services.auth_service import ANONYMOUS_ADMIN, CSRF_HEADER, SESSION_COOKIE, AuthUser, audit_log

logger = logging.getLogger(__name__)

PUBLIC = {
    "/api/v1/auth/status",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
}
# Public certificate, fetched with a plain `curl` from the registry page's commands
PUBLIC_RE = re.compile(r"^/api/v1/groups/\d+/registry/ca\.crt$")
# Viewers may still manage their own session and API tokens
VIEWER_WRITES_RE = re.compile(r"^/api/v1/auth/(logout|tokens(/\d+)?)$")
# Reads that hand out credentials: admins only
SECRET_READS_RE = re.compile(
    r"^/api/v1/(clusters/\d+/(kubeconfig|openshift/(credentials|ssh-key))"
    r"|groups/\d+/(registry/credentials|wireguard/peers/[^/]+/config))$")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Secrets inside ordinary JSON reads (group specs: members' login password, proxy credentials, raw cloud-init):
# masked for viewers
SECRET_KEYS = {"password", "user_data", "url_with_auth", "private_key", "kubeadmin_password", "token", "secret"}
URL_CREDS_RE = re.compile(r"(//)[^/@\s:]+:[^/@\s]+@")
REDACTED = "***"


def redact(value):
    """Viewer copy of a JSON value: secret keys masked, user:password@ removed from URLs"""
    if isinstance(value, dict):
        return {k: (REDACTED if k in SECRET_KEYS and value[k] not in (None, "") else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and "@" in value:
        return URL_CREDS_RE.sub(r"\1***@", value)
    return value


def _headers(scope) -> Dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def client_ip(scope) -> str:
    client = scope.get("client")
    return client[0] if client else ""


def _same_origin(origin: str, headers: Dict[str, str]) -> bool:
    if origin in settings.CORS_ORIGINS:
        return True
    try:
        return urlsplit(origin).netloc.lower() == headers.get("host", "").lower()
    except ValueError:
        return False


def _resolve(headers: Dict[str, str], ip: str) -> Tuple[Optional[AuthUser], str]:
    """(user, how it failed). Runs in a worker thread: DB + NSS lookups."""
    auth = headers.get("authorization", "")
    with SessionLocal() as db:
        if auth:
            scheme, _, value = auth.partition(" ")
            if scheme.lower() != "bearer" or not value.strip():
                return None, "unsupported Authorization header (use: Bearer <token>)"
            row = auth_service.token_row(db, value.strip(), ip)
            if row is None:
                return None, "invalid or expired API token"
            role = auth_service.role_of(row.username)
            if role is None:
                return None, f"user {row.username} is no longer allowed (not in an admin/viewer group)"
            return AuthUser(row.username, role, "token", token_id=row.id), ""
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
        except Exception:
            pass
        morsel = cookie.get(SESSION_COOKIE)
        if morsel is None:
            return None, "not authenticated"
        username = auth_service.session_user(db, morsel.value)
        if username is None:
            return None, "session expired"
        role = auth_service.role_of(username)
        if role is None:
            return None, f"user {username} is no longer allowed (not in an admin/viewer group)"
        return AuthUser(username, role, "session"), ""


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/") or "/"
        method = scope.get("method", "GET") if scope["type"] == "http" else "WEBSOCKET"
        headers = _headers(scope)
        ip = client_ip(scope)
        state = scope.setdefault("state", {})

        if not settings.AUTH_ENABLED:
            state["user"] = ANONYMOUS_ADMIN
            return await self.app(scope, receive, send)

        public = path in PUBLIC or bool(PUBLIC_RE.match(path))
        user, why = None, "not authenticated"
        if "authorization" in headers or SESSION_COOKIE in headers.get("cookie", ""):
            user, why = await to_thread.run_sync(_resolve, headers, ip)
        state["user"] = user

        if user is None and not public:
            return await self._deny(scope, receive, send, 401, why)

        # CSRF: a cookie is sent by the browser on its own; a Bearer token never is
        bearer = user is not None and user.via == "token"
        origin = headers.get("origin")
        if scope["type"] == "websocket":
            if not bearer and (not origin or not _same_origin(origin, headers)):
                return await self._deny(scope, receive, send, 403, "cross-origin WebSocket refused")
        elif method not in SAFE_METHODS and not bearer:
            if origin and not _same_origin(origin, headers):
                return await self._deny(scope, receive, send, 403, "cross-origin request refused")
            if CSRF_HEADER not in headers:
                return await self._deny(scope, receive, send, 403, f"missing {CSRF_HEADER} header "
                                        f"(browser writes need it; scripts use Authorization: Bearer)")

        if user is not None and user.role != "admin" and not public:
            writes = method not in SAFE_METHODS
            if (writes and not VIEWER_WRITES_RE.match(path)) or SECRET_READS_RE.match(path):
                return await self._deny(scope, receive, send, 403, "read-only account (viewer)")

        if (user is not None and user.role != "admin" and scope["type"] == "http" and method == "GET"
                and not public):
            return await self._viewer_read(scope, receive, send)

        if method in SAFE_METHODS or user is None:
            return await self.app(scope, receive, send)

        # audit: who changed what, with the outcome
        status = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            elif message["type"] == "websocket.accept":
                status["code"] = 101
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            query = scope.get("query_string", b"").decode("latin-1")
            audit_log.info(f"user={user.name} role={user.role} via={user.via}"
                           f"{f' token={user.token_id}' if user.token_id else ''} ip={ip} "
                           f"{method} {scope['path']}{'?' + query if query else ''} -> {status['code'] or 'error'}")

    async def _viewer_read(self, scope, receive, send):
        """Pass the response through redact() when it is JSON (the SSE stream and files stream unchanged)"""
        start = {}
        chunks = []

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                ctype = dict(message.get("headers") or []).get(b"content-type", b"")
                if not ctype.startswith(b"application/json"):
                    start["passthrough"] = True
                    return await send(message)
                start["message"] = message
                return None
            if start.get("passthrough"):
                return await send(message)
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return None
            body = b"".join(chunks)
            try:
                body = json.dumps(redact(json.loads(body))).encode()
            except ValueError:
                pass
            headers = [(k, v) for k, v in start["message"].get("headers") or [] if k != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode()))
            await send({**start["message"], "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, send_wrapper)

    async def _deny(self, scope, receive, send, code: int, detail: str):
        if code == 403:
            audit_log.info(f"denied ip={ip_or(scope)} {scope.get('method', 'WEBSOCKET')} {scope['path']}: {detail}")
        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the handshake with HTTP 403
            await send({"type": "websocket.close", "code": 4401 if code == 401 else 4403, "reason": detail})
            return
        body = json.dumps({"detail": detail, **({"auth": "required"} if code == 401 else {})}).encode()
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})


def ip_or(scope) -> str:
    return client_ip(scope) or "?"
