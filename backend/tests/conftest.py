"""Test setup: a throw-away SQLite DB + data dir, libvirt's test driver, authentication on, set before any app
import. Roles are faked (no dependency on the host's users / groups): see `roles`.

Run: cd backend && venv/bin/python -m pytest tests
"""
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="vmm-tests-")
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "DATA_DIR": f"{_tmp}/data",
    "LIBVIRT_URI": "test:///default",
    "WG_RELAY_LISTEN": "127.0.0.2",
    "AUTH_ENABLED": "true",
    "HOST_DNS_ENABLED": "false",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.services import auth_service  # noqa: E402

init_db()

ROLES = {"alice": "admin", "victor": "viewer"}


@pytest.fixture(autouse=True)
def roles(monkeypatch):
    """alice = admin, victor = viewer, anyone else = not allowed"""
    monkeypatch.setattr(auth_service, "role_of", lambda name, cached=True: ROLES.get(name))
    auth_service.limiter.__init__()
    return ROLES


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from app.main import app
    return TestClient(app, base_url="http://testserver")


def _token(user: str) -> str:
    with SessionLocal() as db:
        return auth_service.create_token(db, user, "test")[1]


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {_token('alice')}"}


@pytest.fixture
def viewer_headers():
    return {"Authorization": f"Bearer {_token('victor')}"}


@pytest.fixture()
def db():
    """A fresh empty database per test"""
    from app import models  # noqa: F401  (registers the tables)
    from app.database import Base, engine
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
