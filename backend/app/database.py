"""Database configuration"""
import threading
from functools import wraps

from sqlalchemy import create_engine, event, inspect, text
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


def init_db() -> None:
    """Create missing tables, then add missing nullable columns to existing ones.

    create_all() never alters an existing table, so a model gaining a column would break
    databases created by an older version. New columns must be nullable (or have a server
    default) so they can be added in place; nothing is ever dropped or rewritten.
    """
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing or not (column.nullable or column.server_default is not None):
                    continue
                ddl = column.type.compile(dialect=engine.dialect)
                default = f" DEFAULT {column.server_default.arg}" if column.server_default is not None else ""
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}{default}'))


def get_db():
    """Database session for FastAPI dependencies"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
