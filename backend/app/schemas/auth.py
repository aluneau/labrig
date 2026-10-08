"""Authentication schemas"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AuthUserOut(BaseModel):
    name: str
    role: str  # admin | viewer
    via: str  # session | token | disabled


class AuthStatus(BaseModel):
    enabled: bool
    user: Optional[AuthUserOut] = None
    # Shown on the login page: which accounts may log in
    admin_groups: List[str] = []
    viewer_groups: List[str] = []


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=4096)


class TokenCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    expires_days: Optional[float] = Field(None, gt=0, le=3650)


class TokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    prefix: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    last_used_ip: Optional[str] = None


class TokenCreated(TokenOut):
    token: str  # shown once
