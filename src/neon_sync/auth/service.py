"""Neon Sync Authentication Service: scrypt passwords, Google OAuth 2.0 + PKCE, Sessions & API Keys."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

import httpx

from neon_sync.config import Settings

from .database import AuthDatabase, SessionRow, UserRow
from .models import (
    ApiKeyCreateResponse,
    ApiKeyPublic,
    AuthActivityEvent,
    AuthSessionPublic,
    UserPublic,
)

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$")
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1


class AuthError(RuntimeError):
    """Structured authentication or authorization failure."""

    def __init__(self, code: str, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class GoogleProfile:
    sub: str
    email: str
    email_verified: bool
    name: str
    avatar_url: str | None = None


def hash_password(password: str) -> str:
    """Hash a plaintext password using stdlib scrypt with a random 16-byte salt."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    dk_b64 = base64.urlsafe_b64encode(dk).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_b64}${dk_b64}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Verify a password against a stored scrypt hash using constant-time comparison."""
    if not stored_hash or not stored_hash.startswith("scrypt$"):
        return False
    parts = stored_hash.split("$")
    if len(parts) != 6:
        return False
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = base64.urlsafe_b64decode(parts[4].encode("ascii"))
        expected = base64.urlsafe_b64decode(parts[5].encode("ascii"))
    except Exception:  # noqa: BLE001
        return False
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
    return hmac.compare_digest(derived, expected)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _safe_redirect_path(path: str | None) -> str:
    if not path or not path.startswith("/") or path.startswith("//"):
        return "/"
    return path


class AuthService:
    """High-level authentication business logic coordinating SQLite DB & Google OAuth."""

    def __init__(
        self,
        settings: Settings,
        db: AuthDatabase,
        *,
        google_code_exchanger: Callable[[str, str, str], Awaitable[GoogleProfile]] | None = None,
        google_id_token_verifier: Callable[[str], Awaitable[GoogleProfile]] | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._google_code_exchanger = google_code_exchanger
        self._google_id_token_verifier = google_id_token_verifier
        self._pro_keys = {k.strip() for k in settings.pro_api_keys.split(",") if k.strip()}

    @property
    def db(self) -> AuthDatabase:
        return self._db

    @property
    def google_enabled(self) -> bool:
        return bool(self._settings.google_client_id and self._settings.google_client_secret)

    def resolve_google_redirect_uri(self, request_origin: str) -> str:
        if self._settings.google_redirect_uri:
            return self._settings.google_redirect_uri.strip()
        base = (self._settings.public_base_url or request_origin).rstrip("/")
        return f"{base}/v1/auth/google/callback"

    # ---------- Email + Password ----------
    def register_with_password(
        self,
        *,
        name: str,
        email: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[UserPublic, str, datetime]:
        clean_email = email.strip().lower()
        if not _EMAIL_RE.match(clean_email):
            raise AuthError("ERR_AUTH_INVALID_EMAIL", "Enter a valid email address.", status_code=400)
        if len(password) < 8:
            raise AuthError(
                "ERR_AUTH_WEAK_PASSWORD",
                "Password must be at least 8 characters long.",
                status_code=400,
            )
        if self._db.get_user_by_email(clean_email) is not None:
            raise AuthError(
                "ERR_AUTH_EMAIL_EXISTS",
                "An account with this email already exists. Try signing in instead.",
                status_code=409,
            )

        user = self._db.create_user(
            email=clean_email,
            name=name.strip(),
            password_hash=hash_password(password),
            email_verified=False,
        )
        raw_token, sess = self._issue_session(user.id, ip_address=ip_address, user_agent=user_agent)
        self._db.record_event(
            user_id=user.id,
            event="register.password",
            ip_address=ip_address,
            user_agent=user_agent,
            detail="Account created with email & password",
        )
        return self._db.to_public_user(user), raw_token, sess.expires_at

    def login_with_password(
        self,
        *,
        email: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[UserPublic, str, datetime]:
        clean_email = email.strip().lower()
        user = self._db.get_user_by_email(clean_email)
        if user is None or not verify_password(password, user.password_hash):
            raise AuthError(
                "ERR_AUTH_INVALID_CREDENTIALS",
                "Invalid email or password.",
                status_code=401,
            )

        updated = self._db.update_user_profile(user.id, touch_login=True) or user
        raw_token, sess = self._issue_session(updated.id, ip_address=ip_address, user_agent=user_agent)
        self._db.record_event(
            user_id=updated.id,
            event="login.password",
            ip_address=ip_address,
            user_agent=user_agent,
            detail="Signed in with email & password",
        )
        return self._db.to_public_user(updated), raw_token, sess.expires_at

    # ---------- Google OAuth 2.0 + PKCE ----------
    def build_google_authorize_url(
        self,
        *,
        request_origin: str,
        redirect_to: str = "/",
    ) -> str:
        if not self.google_enabled:
            raise AuthError(
                "ERR_OAUTH_NOT_CONFIGURED",
                "Google Sign-In is not configured on this server yet. Set NEON_SYNC_GOOGLE_CLIENT_ID and NEON_SYNC_GOOGLE_CLIENT_SECRET.",
                status_code=503,
            )

        state = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(48)
        challenge = _pkce_challenge(code_verifier)
        safe_next = _safe_redirect_path(redirect_to)
        self._db.save_oauth_state(state=state, code_verifier=code_verifier, redirect_to=safe_next)

        redirect_uri = self.resolve_google_redirect_uri(request_origin)
        params = {
            "client_id": self._settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"

    async def complete_google_callback(
        self,
        *,
        code: str,
        state: str,
        request_origin: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[UserPublic, str, datetime, str]:
        if not self.google_enabled:
            raise AuthError(
                "ERR_OAUTH_NOT_CONFIGURED",
                "Google Sign-In is not configured on this server.",
                status_code=503,
            )

        consumed = self._db.consume_oauth_state(state)
        if consumed is None:
            raise AuthError(
                "ERR_OAUTH_INVALID_STATE",
                "Sign-in session expired or state mismatch. Please try again.",
                status_code=400,
            )
        code_verifier, redirect_to = consumed
        redirect_uri = self.resolve_google_redirect_uri(request_origin)

        if self._google_code_exchanger is not None:
            profile = await self._google_code_exchanger(code, code_verifier, redirect_uri)
        else:
            profile = await self._exchange_google_code(code, code_verifier, redirect_uri)

        user, raw_token, expires_at = self._upsert_google_user_and_session(
            profile,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return user, raw_token, expires_at, redirect_to

    async def login_with_google_id_token(
        self,
        *,
        credential: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[UserPublic, str, datetime]:
        if not self._settings.google_client_id:
            raise AuthError(
                "ERR_OAUTH_NOT_CONFIGURED",
                "Google Sign-In is not configured on this server.",
                status_code=503,
            )
        if self._google_id_token_verifier is not None:
            profile = await self._google_id_token_verifier(credential)
        else:
            profile = await self._verify_google_id_token(credential)

        return self._upsert_google_user_and_session(
            profile,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    async def _exchange_google_code(
        self,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> GoogleProfile:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                token_resp = await client.post(
                    _GOOGLE_TOKEN_URL,
                    data={
                        "code": code,
                        "client_id": self._settings.google_client_id,
                        "client_secret": self._settings.google_client_secret,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                        "code_verifier": code_verifier,
                    },
                )
                if token_resp.status_code != 200:
                    raise AuthError(
                        "ERR_OAUTH_EXCHANGE_FAILED",
                        "Google authorization exchange failed. Please try signing in again.",
                        status_code=401,
                    )
                tokens = token_resp.json()
                access_token = tokens.get("access_token")
                if not access_token:
                    raise AuthError(
                        "ERR_OAUTH_EXCHANGE_FAILED",
                        "Google did not return a valid access token.",
                        status_code=401,
                    )

                info_resp = await client.get(
                    _GOOGLE_USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if info_resp.status_code != 200:
                    raise AuthError(
                        "ERR_OAUTH_USERINFO_FAILED",
                        "Could not verify Google account profile.",
                        status_code=401,
                    )
                info = info_resp.json()
        except AuthError:
            raise
        except httpx.HTTPError as exc:
            raise AuthError(
                "ERR_OAUTH_NETWORK",
                "Could not reach Google authentication servers.",
                status_code=502,
            ) from exc

        sub = str(info.get("sub") or "").strip()
        email = str(info.get("email") or "").strip().lower()
        verified = bool(info.get("email_verified"))
        if not sub or not email:
            raise AuthError(
                "ERR_OAUTH_PROFILE_INCOMPLETE",
                "Google account did not provide required identity fields.",
                status_code=400,
            )
        name = str(info.get("name") or email.split("@")[0]).strip()
        picture = info.get("picture")
        return GoogleProfile(
            sub=sub,
            email=email,
            email_verified=verified,
            name=name,
            avatar_url=str(picture) if picture else None,
        )

    async def _verify_google_id_token(self, credential: str) -> GoogleProfile:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(_GOOGLE_TOKENINFO_URL, params={"id_token": credential})
                if resp.status_code != 200:
                    raise AuthError(
                        "ERR_OAUTH_INVALID_TOKEN",
                        "Google ID token verification failed.",
                        status_code=401,
                    )
                info = resp.json()
        except AuthError:
            raise
        except httpx.HTTPError as exc:
            raise AuthError(
                "ERR_OAUTH_NETWORK",
                "Could not reach Google token verification service.",
                status_code=502,
            ) from exc

        if info.get("aud") != self._settings.google_client_id:
            raise AuthError("ERR_OAUTH_AUDIENCE_MISMATCH", "Google token audience mismatch.", status_code=401)
        if info.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
            raise AuthError("ERR_OAUTH_ISSUER_MISMATCH", "Invalid Google token issuer.", status_code=401)

        sub = str(info.get("sub") or "").strip()
        email = str(info.get("email") or "").strip().lower()
        verified = str(info.get("email_verified", "")).lower() in {"true", "1"}
        if not sub or not email or not verified:
            raise AuthError(
                "ERR_OAUTH_UNVERIFIED_EMAIL",
                "Google account must have a verified email address.",
                status_code=401,
            )
        name = str(info.get("name") or email.split("@")[0]).strip()
        picture = info.get("picture")
        return GoogleProfile(
            sub=sub,
            email=email,
            email_verified=True,
            name=name,
            avatar_url=str(picture) if picture else None,
        )

    def _upsert_google_user_and_session(
        self,
        profile: GoogleProfile,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[UserPublic, str, datetime]:
        user = self._db.get_user_by_oauth("google", profile.sub)
        if user is None:
            # Link cleanly to an existing account with the same verified email
            existing = self._db.get_user_by_email(profile.email) if profile.email_verified else None
            if existing is not None:
                user = (
                    self._db.update_user_profile(
                        existing.id,
                        avatar_url=existing.avatar_url or profile.avatar_url,
                        email_verified=True,
                        touch_login=True,
                    )
                    or existing
                )
                self._db.link_oauth_account(
                    user_id=user.id,
                    provider="google",
                    provider_user_id=profile.sub,
                    provider_email=profile.email,
                )
            else:
                user = self._db.create_user(
                    email=profile.email,
                    name=profile.name,
                    avatar_url=profile.avatar_url,
                    email_verified=profile.email_verified,
                )
                self._db.link_oauth_account(
                    user_id=user.id,
                    provider="google",
                    provider_user_id=profile.sub,
                    provider_email=profile.email,
                )
        else:
            user = (
                self._db.update_user_profile(
                    user.id,
                    avatar_url=profile.avatar_url or user.avatar_url,
                    email_verified=user.email_verified or profile.email_verified,
                    touch_login=True,
                )
                or user
            )

        raw_token, sess = self._issue_session(user.id, ip_address=ip_address, user_agent=user_agent)
        self._db.record_event(
            user_id=user.id,
            event="login.google",
            ip_address=ip_address,
            user_agent=user_agent,
            detail=f"Signed in with Google ({profile.email})",
        )
        return self._db.to_public_user(user), raw_token, sess.expires_at

    # ---------- Session Resolution & Management ----------
    def _issue_session(
        self,
        user_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[str, SessionRow]:
        raw_token = secrets.token_urlsafe(32)
        sess = self._db.create_session(
            user_id=user_id,
            token_hash=hash_token(raw_token),
            ttl_seconds=self._settings.auth_session_ttl_seconds,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return raw_token, sess

    def authenticate_cookie(self, raw_cookie: str | None) -> tuple[SessionRow, UserRow] | None:
        if not raw_cookie or len(raw_cookie) < 16:
            return None
        return self._db.resolve_session(hash_token(raw_cookie.strip()))

    def authenticate_api_key(self, raw_key: str | None) -> tuple[str, UserRow] | None:
        if not raw_key or not raw_key.startswith("ns_live_"):
            return None
        return self._db.resolve_api_key(hash_token(raw_key.strip()))

    def logout_session(
        self,
        raw_cookie: str | None,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> bool:
        resolved = self.authenticate_cookie(raw_cookie)
        if resolved is None:
            return False
        sess, user = resolved
        ok = self._db.revoke_session(sess.id, user_id=user.id)
        if ok:
            self._db.record_event(
                user_id=user.id,
                event="logout",
                ip_address=ip_address,
                user_agent=user_agent,
                detail="Signed out current session",
            )
        return ok

    def list_sessions(self, user_id: str, *, current_session_id: str | None = None) -> list[AuthSessionPublic]:
        return self._db.list_user_sessions(user_id, current_session_id=current_session_id)

    def revoke_session(
        self,
        user_id: str,
        session_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> bool:
        ok = self._db.revoke_session(session_id, user_id=user_id)
        if ok:
            self._db.record_event(
                user_id=user_id,
                event="session.revoked",
                ip_address=ip_address,
                user_agent=user_agent,
                detail=f"Revoked session {session_id}",
            )
        return ok

    def revoke_other_sessions(
        self,
        user_id: str,
        *,
        keep_session_id: str | None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> int:
        count = self._db.revoke_other_sessions(user_id, keep_session_id=keep_session_id)
        if count > 0:
            self._db.record_event(
                user_id=user_id,
                event="session.revoke_others",
                ip_address=ip_address,
                user_agent=user_agent,
                detail=f"Signed out {count} other active session(s)",
            )
        return count

    # ---------- Profile, Password & Plan Upgrade ----------
    def update_profile(
        self,
        user_id: str,
        *,
        name: str,
        avatar_url: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> UserPublic:
        updated = self._db.update_user_profile(user_id, name=name, avatar_url=avatar_url)
        if updated is None:
            raise AuthError("ERR_NOT_FOUND", "User account not found.", status_code=404)
        self._db.record_event(
            user_id=user_id,
            event="profile.updated",
            ip_address=ip_address,
            user_agent=user_agent,
            detail="Updated account profile",
        )
        return self._db.to_public_user(updated)

    def change_password(
        self,
        user_id: str,
        *,
        current_password: str | None,
        new_password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> UserPublic:
        user = self._db.get_user_by_id(user_id)
        if user is None:
            raise AuthError("ERR_NOT_FOUND", "User account not found.", status_code=404)
        if user.password_hash and (
            not current_password or not verify_password(current_password, user.password_hash)
        ):
            raise AuthError(
                "ERR_AUTH_INVALID_CREDENTIALS",
                "Current password is incorrect.",
                status_code=401,
            )
        if len(new_password) < 8:
            raise AuthError(
                "ERR_AUTH_WEAK_PASSWORD",
                "New password must be at least 8 characters long.",
                status_code=400,
            )
        updated = self._db.update_user_profile(user_id, password_hash=hash_password(new_password)) or user
        self._db.record_event(
            user_id=user_id,
            event="password.updated",
            ip_address=ip_address,
            user_agent=user_agent,
            detail="Password updated",
        )
        return self._db.to_public_user(updated)

    def upgrade_account_with_pro_key(
        self,
        user_id: str,
        *,
        pro_key: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> UserPublic:
        if pro_key.strip() not in self._pro_keys:
            raise AuthError("ERR_INVALID_PRO_KEY", "Pro access key not recognized.", status_code=403)
        updated = self._db.update_user_profile(user_id, tier="pro")
        if updated is None:
            raise AuthError("ERR_NOT_FOUND", "User account not found.", status_code=404)
        self._db.record_event(
            user_id=user_id,
            event="account.upgraded",
            ip_address=ip_address,
            user_agent=user_agent,
            detail="Upgraded account to Neon Pro",
        )
        return self._db.to_public_user(updated)

    # ---------- Developer API Keys ----------
    def create_api_key(
        self,
        user_id: str,
        *,
        name: str,
        scopes: list[str],
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> ApiKeyCreateResponse:
        raw_secret = f"ns_live_{secrets.token_urlsafe(28)}"
        prefix = raw_secret[:14]
        key_pub = self._db.create_api_key(
            user_id=user_id,
            name=name,
            key_prefix=prefix,
            key_hash=hash_token(raw_secret),
            scopes=scopes,
        )
        self._db.record_event(
            user_id=user_id,
            event="api_key.created",
            ip_address=ip_address,
            user_agent=user_agent,
            detail=f"Created API key '{name.strip()}' ({prefix}…)",
        )
        return ApiKeyCreateResponse(api_key=key_pub, secret=raw_secret)

    def list_api_keys(self, user_id: str) -> list[ApiKeyPublic]:
        return self._db.list_api_keys(user_id)

    def revoke_api_key(
        self,
        user_id: str,
        key_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> bool:
        ok = self._db.revoke_api_key(key_id, user_id=user_id)
        if ok:
            self._db.record_event(
                user_id=user_id,
                event="api_key.revoked",
                ip_address=ip_address,
                user_agent=user_agent,
                detail=f"Revoked API key {key_id}",
            )
        return ok

    def list_activity(self, user_id: str, *, limit: int = 30) -> list[AuthActivityEvent]:
        return self._db.list_events(user_id, limit=limit)
