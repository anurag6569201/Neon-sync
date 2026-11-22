"""Neon Sync Authentication & Account API routes (/v1/auth/*)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from neon_sync.auth import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyPublic,
    AuthActivityEvent,
    AuthError,
    AuthProvidersResponse,
    AuthResponse,
    AuthSessionPublic,
    GoogleIdTokenRequest,
    LoginRequest,
    MeResponse,
    PasswordChangeRequest,
    ProfileUpdateRequest,
    RegisterRequest,
    UpgradeAccountRequest,
    UserPublic,
)
from neon_sync.auth.database import SessionRow, UserRow
from neon_sync.cloud import TurnstileVerificationError, verify_turnstile_token

from .branding import brand_error_detail

if TYPE_CHECKING:
    from .app import AppContainer

auth_router = APIRouter(prefix="/auth", tags=["auth"])


@dataclass
class RequestAuthContext:
    user: UserRow | None = None
    session: SessionRow | None = None
    api_key_id: str | None = None
    method: Literal["session", "api_key", "guest"] = "guest"


def _container(request: Request) -> AppContainer:
    return request.app.state.container


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _request_origin(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _extract_api_key(request: Request) -> str | None:
    auth_hdr = request.headers.get("authorization", "")
    if auth_hdr.lower().startswith("bearer "):
        tok = auth_hdr[7:].strip()
        if tok.startswith("ns_live_"):
            return tok
    x_key = request.headers.get("x-api-key", "").strip()
    if x_key.startswith("ns_live_"):
        return x_key
    return None


def resolve_request_auth(request: Request) -> RequestAuthContext:
    """Resolve authenticated user from HttpOnly session cookie or Bearer/X-Api-Key token."""
    cached = getattr(request.state, "_auth_ctx", None)
    if isinstance(cached, RequestAuthContext):
        return cached

    c = _container(request)
    # 1. Check session cookie first
    raw_cookie = request.cookies.get(c.settings.auth_cookie_name)
    if raw_cookie:
        resolved = c.auth.authenticate_cookie(raw_cookie)
        if resolved is not None:
            sess, user = resolved
            ctx = RequestAuthContext(user=user, session=sess, method="session")
            request.state._auth_ctx = ctx
            return ctx

    # 2. Check Developer API Key (Authorization: Bearer ns_live_... or X-Api-Key)
    raw_key = _extract_api_key(request)
    if raw_key:
        resolved_key = c.auth.authenticate_api_key(raw_key)
        if resolved_key is not None:
            key_id, user = resolved_key
            ctx = RequestAuthContext(user=user, api_key_id=key_id, method="api_key")
            request.state._auth_ctx = ctx
            return ctx

    ctx = RequestAuthContext()
    request.state._auth_ctx = ctx
    return ctx


def _require_auth(request: Request) -> RequestAuthContext:
    ctx = resolve_request_auth(request)
    if ctx.user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=brand_error_detail(
                "ERR_AUTH_UNAUTHENTICATED",
                "Sign in to access this Neon Sync account resource.",
                request_id=_request_id(request),
            ),
        )
    return ctx


def _set_session_cookie(response: Response, request: Request, raw_token: str) -> None:
    cfg = _container(request).settings
    is_https = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
    secure = cfg.auth_cookie_secure or cfg.environment == "production" or is_https
    response.set_cookie(
        key=cfg.auth_cookie_name,
        value=raw_token,
        max_age=cfg.auth_session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    cfg = _container(request).settings
    response.delete_cookie(key=cfg.auth_cookie_name, path="/")


def _raise_auth_error(exc: AuthError, request: Request) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail=brand_error_detail(
            exc.code,
            exc.message,
            request_id=_request_id(request),
        ),
    ) from exc


@auth_router.get("/providers", response_model=AuthProvidersResponse)
async def get_auth_providers(request: Request) -> AuthProvidersResponse:
    """Return available Neon Sync sign-in providers, Google OAuth & Cloudflare Turnstile readiness."""
    c = _container(request)
    ts_enabled = bool(c.settings.cf_turnstile_site_key and c.settings.cf_turnstile_secret)
    return AuthProvidersResponse(
        google_enabled=c.auth.google_enabled,
        google_client_id=c.settings.google_client_id if c.auth.google_enabled else None,
        google_redirect_uri=c.auth.resolve_google_redirect_uri(_request_origin(request)),
        password_enabled=True,
        turnstile_enabled=ts_enabled,
        turnstile_site_key=c.settings.cf_turnstile_site_key if ts_enabled else None,
    )


@auth_router.get("/me", response_model=MeResponse)
async def get_me(request: Request) -> MeResponse:
    """Return the currently signed-in Neon Sync user, or guest status."""
    ctx = resolve_request_auth(request)
    if ctx.user is None:
        return MeResponse(authenticated=False, user=None, auth_method="guest")
    c = _container(request)
    return MeResponse(
        authenticated=True,
        user=c.auth.db.to_public_user(ctx.user),
        auth_method=ctx.method,
    )


@auth_router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, request: Request, response: Response) -> AuthResponse:
    """Create a new Neon Sync user account in the database and establish a secure session."""
    c = _container(request)
    try:
        await verify_turnstile_token(
            token=request.headers.get("cf-turnstile-response"),
            expected_action="signup",
            client_ip=_client_ip(request),
            settings=c.settings,
        )
    except TurnstileVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=brand_error_detail("ERR_BOT_VERIFICATION_FAILED", exc.message, request_id=_request_id(request)),
        ) from exc
    try:
        user, raw_token, expires_at = c.auth.register_with_password(
            name=body.name,
            email=body.email,
            password=body.password,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
    except AuthError as exc:
        _raise_auth_error(exc, request)
    _set_session_cookie(response, request, raw_token)
    return AuthResponse(authenticated=True, user=user, session_expires_at=expires_at)


@auth_router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, request: Request, response: Response) -> AuthResponse:
    """Sign in with email and password and establish an HttpOnly session cookie."""
    c = _container(request)
    try:
        user, raw_token, expires_at = c.auth.login_with_password(
            email=body.email,
            password=body.password,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
    except AuthError as exc:
        _raise_auth_error(exc, request)
    _set_session_cookie(response, request, raw_token)
    return AuthResponse(authenticated=True, user=user, session_expires_at=expires_at)


@auth_router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    """Revoke the current server-side session and clear the session cookie."""
    c = _container(request)
    raw_cookie = request.cookies.get(c.settings.auth_cookie_name)
    c.auth.logout_session(raw_cookie, ip_address=_client_ip(request), user_agent=_user_agent(request))
    _clear_session_cookie(response, request)
    return {"status": "ok", "service": "neon-sync", "authenticated": False}


@auth_router.get("/google/start")
async def google_oauth_start(
    request: Request,
    next_path: str = Query(default="/", alias="next"),
) -> RedirectResponse:
    """Begin Google Sign-In OAuth 2.0 + PKCE (S256) authorization redirect."""
    c = _container(request)
    try:
        auth_url = c.auth.build_google_authorize_url(
            request_origin=_request_origin(request),
            redirect_to=next_path,
        )
    except AuthError as exc:
        _raise_auth_error(exc, request)
    return RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)


@auth_router.get("/google/callback")
async def google_oauth_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Handle Google OAuth 2.0 callback, persist user in DB, issue session cookie, and return to app."""
    if error:
        qs = urlencode({"auth_error": error})
        return RedirectResponse(url=f"/?{qs}", status_code=status.HTTP_302_FOUND)
    if not code or not state:
        qs = urlencode({"auth_error": "missing_oauth_parameters"})
        return RedirectResponse(url=f"/?{qs}", status_code=status.HTTP_302_FOUND)

    c = _container(request)
    try:
        _user, raw_token, _expires_at, redirect_to = await c.auth.complete_google_callback(
            code=code,
            state=state,
            request_origin=_request_origin(request),
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
    except AuthError as exc:
        qs = urlencode({"auth_error": exc.code})
        return RedirectResponse(url=f"/?{qs}", status_code=status.HTTP_302_FOUND)

    sep = "&" if "?" in redirect_to else "?"
    resp = RedirectResponse(url=f"{redirect_to}{sep}auth=google_ok", status_code=status.HTTP_302_FOUND)
    _set_session_cookie(resp, request, raw_token)
    return resp


@auth_router.post("/google/token", response_model=AuthResponse)
async def google_id_token_signin(
    body: GoogleIdTokenRequest,
    request: Request,
    response: Response,
) -> AuthResponse:
    """Verify a Google Identity Services ID token (One Tap / GIS button) and create a session."""
    c = _container(request)
    try:
        user, raw_token, expires_at = await c.auth.login_with_google_id_token(
            credential=body.credential,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
    except AuthError as exc:
        _raise_auth_error(exc, request)
    _set_session_cookie(response, request, raw_token)
    return AuthResponse(authenticated=True, user=user, session_expires_at=expires_at)


@auth_router.patch("/me", response_model=UserPublic)
async def update_my_profile(body: ProfileUpdateRequest, request: Request) -> UserPublic:
    """Update display name or avatar URL for the signed-in Neon Sync account."""
    ctx = _require_auth(request)
    assert ctx.user is not None
    c = _container(request)
    try:
        return c.auth.update_profile(
            ctx.user.id,
            name=body.name,
            avatar_url=body.avatar_url,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
    except AuthError as exc:
        _raise_auth_error(exc, request)


@auth_router.post("/password", response_model=UserPublic)
async def update_my_password(body: PasswordChangeRequest, request: Request) -> UserPublic:
    """Change password or add password login to a Google-connected Neon Sync account."""
    ctx = _require_auth(request)
    assert ctx.user is not None
    c = _container(request)
    try:
        return c.auth.change_password(
            ctx.user.id,
            current_password=body.current_password,
            new_password=body.new_password,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
    except AuthError as exc:
        _raise_auth_error(exc, request)


@auth_router.post("/upgrade", response_model=UserPublic)
async def upgrade_account(body: UpgradeAccountRequest, request: Request) -> UserPublic:
    """Activate Neon Pro permanently on the signed-in account in the database."""
    ctx = _require_auth(request)
    assert ctx.user is not None
    c = _container(request)
    try:
        return c.auth.upgrade_account_with_pro_key(
            ctx.user.id,
            pro_key=body.pro_key,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
    except AuthError as exc:
        _raise_auth_error(exc, request)


@auth_router.get("/sessions", response_model=list[AuthSessionPublic])
async def list_my_sessions(request: Request) -> list[AuthSessionPublic]:
    """List all active browser/device sessions for the signed-in user."""
    ctx = _require_auth(request)
    assert ctx.user is not None
    return _container(request).auth.list_sessions(
        ctx.user.id,
        current_session_id=ctx.session.id if ctx.session else None,
    )


@auth_router.delete("/sessions/{session_id}")
async def revoke_my_session(session_id: str, request: Request) -> dict:
    """Revoke a specific active session."""
    ctx = _require_auth(request)
    assert ctx.user is not None
    ok = _container(request).auth.revoke_session(
        ctx.user.id,
        session_id,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "Session not found", request_id=_request_id(request)),
        )
    return {"status": "ok", "service": "neon-sync", "revoked": session_id}


@auth_router.post("/sessions/revoke-others")
async def revoke_other_sessions(request: Request) -> dict:
    """Sign out all active sessions except the current one."""
    ctx = _require_auth(request)
    assert ctx.user is not None
    count = _container(request).auth.revoke_other_sessions(
        ctx.user.id,
        keep_session_id=ctx.session.id if ctx.session else None,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    return {"status": "ok", "service": "neon-sync", "revoked_count": count}


@auth_router.get("/api-keys", response_model=list[ApiKeyPublic])
async def list_my_api_keys(request: Request) -> list[ApiKeyPublic]:
    """List personal Neon Sync developer API keys (masked prefixes only)."""
    ctx = _require_auth(request)
    assert ctx.user is not None
    return _container(request).auth.list_api_keys(ctx.user.id)


@auth_router.post("/api-keys", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_my_api_key(body: ApiKeyCreateRequest, request: Request) -> ApiKeyCreateResponse:
    """Generate a new personal Neon Sync API key (`ns_live_...`). The full secret is returned once."""
    ctx = _require_auth(request)
    assert ctx.user is not None
    return _container(request).auth.create_api_key(
        ctx.user.id,
        name=body.name,
        scopes=list(body.scopes),
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )


@auth_router.delete("/api-keys/{key_id}")
async def revoke_my_api_key(key_id: str, request: Request) -> dict:
    """Revoke a personal Neon Sync developer API key immediately."""
    ctx = _require_auth(request)
    assert ctx.user is not None
    ok = _container(request).auth.revoke_api_key(
        ctx.user.id,
        key_id,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=brand_error_detail("ERR_NOT_FOUND", "API key not found", request_id=_request_id(request)),
        )
    return {"status": "ok", "service": "neon-sync", "revoked": key_id}


@auth_router.get("/activity", response_model=list[AuthActivityEvent])
async def list_my_auth_activity(request: Request, limit: int = Query(default=25, ge=1, le=100)) -> list[AuthActivityEvent]:
    """Return recent security & sign-in activity events for the signed-in account."""
    ctx = _require_auth(request)
    assert ctx.user is not None
    return _container(request).auth.list_activity(ctx.user.id, limit=limit)
