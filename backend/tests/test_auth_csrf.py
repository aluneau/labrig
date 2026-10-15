"""Cookie-authenticated writes: only from the app's own origin"""
import pytest

from app.config import settings
from app.database import SessionLocal
from app.services import auth_service


@pytest.fixture
def cookie_client(client):
    with SessionLocal() as db:
        value = auth_service.create_session(db, "alice", "127.0.0.1", "test")
    client.cookies.set(auth_service.SESSION_COOKIE, value)
    return client


def _mint_token(c, origin):
    return c.post("/api/v1/auth/tokens", json={"name": "x"},
                  headers={"Origin": origin, "X-VMM-Request": "1"})


def test_same_origin_write_allowed(cookie_client):
    assert _mint_token(cookie_client, "http://testserver").status_code == 201


@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://localhost:5173", "https://evil.example"])
def test_other_origins_refused_by_default(cookie_client, origin):
    """localhost:<other port> is same-site for SameSite=strict: the dev origins must not be trusted in production"""
    assert settings.CORS_ORIGINS == []
    r = _mint_token(cookie_client, origin)
    assert r.status_code == 403 and "token" not in r.json()
    pre = cookie_client.options("/api/v1/auth/tokens", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-vmm-request"})
    assert "access-control-allow-origin" not in pre.headers


def test_write_without_csrf_header_refused(cookie_client):
    r = cookie_client.post("/api/v1/auth/tokens", json={"name": "x"})
    assert r.status_code == 403
