"""Login / logout / status and API tokens (docs/auth.md)"""
import asyncio
import pwd
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.auth import ApiToken
from app.schemas.auth import AuthStatus, AuthUserOut, LoginRequest, TokenCreate, TokenCreated, TokenOut
from app.services import auth_service
from app.services.auth_service import SESSION_COOKIE, AuthUser, audit_log, limiter
from app.auth_middleware import client_ip

router = APIRouter()

FAILURE_FLOOR = 1.5  # seconds: unknown users answer as slowly as PAM's failure delay


def current_user(request: Request) -> Optional[AuthUser]:
    return request.scope.get("state", {}).get("user")


def require_user(request: Request) -> AuthUser:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def _secure(request: Request) -> bool:
    if settings.AUTH_COOKIE_SECURE.lower() in ("true", "1", "yes"):
        return True
    if settings.AUTH_COOKIE_SECURE.lower() in ("false", "0", "no"):
        return False
    return request.url.scheme == "https"


def _split(groups: str) -> List[str]:
    return [g.strip() for g in groups.split(",") if g.strip()]


@router.get("/status", response_model=AuthStatus)
def auth_status(request: Request):
    """Public: is login required, and who am I (null when not logged in)"""
    user = current_user(request)
    return AuthStatus(
        enabled=settings.AUTH_ENABLED,
        user=AuthUserOut(name=user.name, role=user.role, via=user.via) if user else None,
        admin_groups=_split(settings.AUTH_ADMIN_GROUPS) if settings.AUTH_ENABLED else [],
        viewer_groups=_split(settings.AUTH_VIEWER_GROUPS) if settings.AUTH_ENABLED else [],
    )


@router.post("/login", response_model=AuthStatus)
async def login(request: Request, response: Response, body: Optional[LoginRequest] = None,
                db: Session = Depends(get_db)):
    """Log in with a Linux account (PAM) and get a session cookie. With an empty body and
    `Authorization: Bearer <token>`, opens a browser session for the token's user (scripts / e2e)."""
    if not settings.AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="authentication is disabled (AUTH_ENABLED=false)")
    ip = client_ip(request.scope)
    user = current_user(request)
    if body is None:
        if user is None or user.via != "token":
            raise HTTPException(status_code=422, detail="username and password are required")
        username, role = user.name, user.role
    else:
        name = body.username.strip()
        wait = limiter.retry_after(ip, name)
        if wait:
            audit_log.info(f"login throttled user={name} ip={ip} retry_after={wait}s")
            raise HTTPException(status_code=429, detail=f"Too many failed attempts: try again in {wait} s",
                                headers={"Retry-After": str(wait)})
        try:
            username = pwd.getpwnam(name).pw_name
        except KeyError:
            username = None
        loop = asyncio.get_running_loop()
        started = loop.time()
        ok, reason = (await run_in_threadpool(auth_service.check_password, username, body.password)
                      if username else (False, "unknown user"))
        if not ok:
            limiter.failed(ip, name)
            await asyncio.sleep(max(0.0, FAILURE_FLOOR - (loop.time() - started)))
            audit_log.info(f"login failed user={name} ip={ip}: {reason}")
            raise HTTPException(status_code=401, detail="Invalid user name or password")
        limiter.succeeded(ip, name)
        role = await run_in_threadpool(auth_service.role_of, username, False)
        if role is None:
            audit_log.info(f"login refused user={username} ip={ip}: not in an admin or viewer group")
            groups = ", ".join(_split(settings.AUTH_ADMIN_GROUPS) + _split(settings.AUTH_VIEWER_GROUPS))
            raise HTTPException(status_code=403, detail=f"{username} is not allowed to use VM Manager: "
                                                        f"ask an admin to add it to one of these groups: {groups}")
    value = await run_in_threadpool(auth_service.create_session, db, username, ip,
                                    request.headers.get("user-agent", ""))
    response.set_cookie(SESSION_COOKIE, value, httponly=True, samesite="strict", secure=_secure(request),
                        path="/", max_age=int(settings.AUTH_SESSION_MAX_DAYS * 86400))
    audit_log.info(f"login user={username} role={role} ip={ip}{' (token)' if body is None else ''}")
    return AuthStatus(enabled=True, user=AuthUserOut(name=username, role=role, via="session"),
                      admin_groups=_split(settings.AUTH_ADMIN_GROUPS), viewer_groups=_split(settings.AUTH_VIEWER_GROUPS))


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    value = request.cookies.get(SESSION_COOKIE)
    if value:
        auth_service.delete_session(db, value)
    response.delete_cookie(SESSION_COOKIE, path="/")
    user = current_user(request)
    if user and settings.AUTH_ENABLED:
        audit_log.info(f"logout user={user.name} ip={client_ip(request.scope)}")


# ---- API tokens -------------------------------------------------------------------------------

def _require_enabled():
    if not settings.AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="authentication is disabled (AUTH_ENABLED=false): "
                                                    "API tokens are not needed")


@router.get("/tokens", response_model=List[TokenOut])
def list_tokens(request: Request, all_users: bool = Query(False, alias="all"), db: Session = Depends(get_db)):
    """My tokens; admins can list everyone's with ?all=true"""
    user = require_user(request)
    query = db.query(ApiToken)
    if not (all_users and user.is_admin):
        query = query.filter(ApiToken.username == user.name)
    return query.order_by(ApiToken.username, ApiToken.id).all()


@router.post("/tokens", response_model=TokenCreated, status_code=201)
def create_token(data: TokenCreate, request: Request, db: Session = Depends(get_db)):
    """New token for the current user. The token is in this response only (stored hashed)."""
    _require_enabled()
    user = require_user(request)
    row, value = auth_service.create_token(db, user.name, data.name.strip(), data.expires_days)
    audit_log.info(f"token created id={row.id} name={row.name!r} user={user.name}")
    return TokenCreated(**TokenOut.model_validate(row).model_dump(), token=value)


@router.delete("/tokens/{token_id}", status_code=204)
def revoke_token(token_id: int, request: Request, db: Session = Depends(get_db)):
    """Revoke one of my tokens (admins: anyone's)"""
    user = require_user(request)
    row = db.get(ApiToken, token_id)
    if row is None or (row.username != user.name and not user.is_admin):
        raise HTTPException(status_code=404, detail="Token not found")
    db.delete(row)
    db.commit()
    audit_log.info(f"token revoked id={token_id} name={row.name!r} owner={row.username} by={user.name}")
