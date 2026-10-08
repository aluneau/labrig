"""Authentication: browser sessions and API tokens of Linux users (passwords stay in PAM)"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.database import Base


class AuthSession(Base):
    """Server-side browser session; the cookie holds a random secret, the DB only its SHA-256"""
    __tablename__ = "auth_sessions"

    id = Column(String(64), primary_key=True)  # sha256 hex of the cookie value
    username = Column(String(255), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    ip = Column(String(64), nullable=True)
    user_agent = Column(String(255), nullable=True)


class ApiToken(Base):
    """Bearer token of a user (scripts, OpenTofu). Shown once at creation, stored hashed."""
    __tablename__ = "api_tokens"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(255), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False)  # sha256 hex
    prefix = Column(String(16), nullable=False)  # first characters, to recognise it in lists
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    last_used_ip = Column(String(64), nullable=True)
