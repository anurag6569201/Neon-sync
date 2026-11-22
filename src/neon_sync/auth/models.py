"""Pydantic schemas for Neon Sync Authentication, Sessions, Google OAuth & API Keys."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class UserPublic(BaseModel):
    id: str
    email: str
    email_verified: bool = False
    name: str
    avatar_url: str | None = None
    role: Literal["user", "admin"] = "user"
    tier: Literal["free", "pro"] = "free"
    providers: list[str] = Field(default_factory=list)
    has_password: bool = False
    created_at: datetime
    last_login_at: datetime | None = None


class AuthSessionPublic(BaseModel):
    id: str
    user_agent: str | None = None
    ip_address: str | None = None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    is_current: bool = False


class ApiKeyPublic(BaseModel):
    id: str
    name: str
    key_prefix: str
    scopes: list[str] = Field(default_factory=lambda: ["read", "write"])
    created_at: datetime
    last_used_at: datetime | None = None
    revoked: bool = False


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scopes: list[Literal["read", "write", "agent"]] = Field(
        default_factory=lambda: ["read", "write", "agent"]
    )


class ApiKeyCreateResponse(BaseModel):
    api_key: ApiKeyPublic
    secret: str


class AuthActivityEvent(BaseModel):
    id: str
    event: str
    ip_address: str | None = None
    user_agent: str | None = None
    detail: str | None = None
    created_at: datetime


class AuthProvidersResponse(BaseModel):
    google_enabled: bool
    google_client_id: str | None = None
    google_redirect_uri: str
    password_enabled: bool = True


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class GoogleIdTokenRequest(BaseModel):
    credential: str = Field(min_length=20, max_length=8192)


class ProfileUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    avatar_url: str | None = Field(default=None, max_length=1024)


class PasswordChangeRequest(BaseModel):
    current_password: str | None = Field(default=None, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class UpgradeAccountRequest(BaseModel):
    pro_key: str = Field(min_length=4, max_length=128)


class AuthResponse(BaseModel):
    authenticated: bool = True
    user: UserPublic
    session_expires_at: datetime


class MeResponse(BaseModel):
    authenticated: bool
    user: UserPublic | None = None
    auth_method: Literal["session", "api_key", "guest"] = "guest"
