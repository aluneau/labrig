"""Database configuration"""
import threading
from functools import wraps

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

settings.DATA_DIR.mkdir(parents=True, exist_ok=True)

if settings.DATABASE_URL.startswith("sqlite"):
    # One connection per session (requests run in a thread pool); WAL lets readers
    # and the writer work concurrently, busy_timeout waits instead of failing.
    engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 15})

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()
else:
    engine = create_engine(settings.DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

_sync_lock = threading.RLock()


def serialized(func):
    """Run a libvirt -> database mirroring function one at a time.

    Parallel requests (e.g. the storage page loading 4 lists at once) would
    otherwise insert the same libvirt object twice.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        with _sync_lock:
            return func(*args, **kwargs)
    return wrapper


def get_db():
    """Database session for FastAPI dependencies"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
