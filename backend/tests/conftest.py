"""Test setup: settings point at a throwaway DATA_DIR / SQLite DB and libvirt's test driver, set before any app import.

Run: cd backend && venv/bin/python -m pytest tests
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="vmm-tests-")
os.environ["DATA_DIR"] = _TMP
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["LIBVIRT_URI"] = "test:///default"
os.environ["AUTH_ENABLED"] = "false"
os.environ["HOST_DNS_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture()
def db():
    """A fresh empty database per test"""
    from app import models  # noqa: F401  (registers the tables)
    from app.database import Base, SessionLocal, engine
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
