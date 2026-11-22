"""Cloudflare Turnstile Siteverify, Workers AI fallback, R2 & Google Cloud readiness helpers."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from neon_sync.config import Settings

log = logging.getLogger("neon_sync.cloud")

_TURNSTILE_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class TurnstileVerificationError(RuntimeError):
    """Raised when Cloudflare Turnstile bot verification fails (fail-closed)."""

    def __init__(self, message: str = "Bot verification failed. Please retry.") -> None:
        super().__init__(message)
        self.message = message


async def verify_turnstile_token(
    *,
    token: str | None,
    expected_action: str,
    client_ip: str | None,
    settings: Settings,
) -> bool:
    """Canonical server-side Cloudflare Turnstile siteverify (fail-closed when enabled).

    If `cf_turnstile_secret` is not configured, verification is treated as disabled (dev mode)
    and returns True. When configured, requires valid token length, HTTP 200 JSON response,
    `success === true`, matching `action`, and approved `hostname`.
    """
    secret = settings.cf_turnstile_secret
    if not secret:
        return True

    expected_hostnames = {
        h.strip().lower() for h in settings.cf_turnstile_hostnames.split(",") if h.strip()
    }
    if not token or not isinstance(token, str) or len(token) > 2048 or not expected_hostnames:
        raise TurnstileVerificationError("Missing or invalid Turnstile verification token.")

    form_data: dict[str, str] = {"secret": secret, "response": token}
    if client_ip and client_ip != "unknown":
        form_data["remoteip"] = client_ip

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _TURNSTILE_SITEVERIFY_URL,
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                raise TurnstileVerificationError("Verification service temporarily unavailable.")
            data = resp.json()
    except TurnstileVerificationError:
        raise
    except Exception as exc:
        log.warning("Cloudflare Turnstile siteverify failed closed: %s", exc)
        raise TurnstileVerificationError("Verification check failed. Please retry.") from exc

    hostname = str(data.get("hostname") or "").strip().lower()
    action = str(data.get("action") or "").strip()
    if (
        data.get("success") is not True
        or (expected_action and action and action != expected_action)
        or (hostname and hostname not in expected_hostnames)
    ):
        raise TurnstileVerificationError("Request failed Cloudflare Turnstile verification.")
    return True


async def run_cloudflare_workers_ai(
    *,
    system_prompt: str,
    user_prompt: str,
    settings: Settings,
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> str | None:
    """Run an inference call on Cloudflare Workers AI when account_id + api_token are configured."""
    if not settings.cf_account_id or not settings.cf_api_token:
        return None

    model = settings.cf_workers_ai_model
    url = f"https://api.cloudflare.com/client/v4/accounts/{settings.cf_account_id}/ai/run/{model}"
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=22.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {settings.cf_api_token}"},
            )
            resp.raise_for_status()
            body = resp.json()
        result = body.get("result") or {}
        text = result.get("response")
        return str(text).strip() if text else None
    except Exception as exc:  # noqa: BLE001
        log.warning("Cloudflare Workers AI fallback failed: %s", exc)
        return None


def get_cloud_integrations_status(settings: Settings) -> list[dict[str, Any]]:
    """Return structured status of Neon Sync's Google & Cloudflare cloud capabilities."""
    return [
        {
            "id": "google-identity",
            "provider": "Google",
            "name": "Google Sign-In (OAuth 2.0 + PKCE)",
            "status": "active" if (settings.google_client_id and settings.google_client_secret) else "standby",
            "detail": "Configured for OAuth 2.0 & One Tap" if settings.google_client_id else "Set NEON_SYNC_GOOGLE_CLIENT_ID in .env",
        },
        {
            "id": "google-gemini-ai",
            "provider": "Google",
            "name": f"Google Gemini AI ({settings.gemini_model})",
            "status": "active" if settings.gemini_api_key else "byok_ready",
            "detail": "Server key active + BYOK supported" if settings.gemini_api_key else "BYOK header ready (or set NEON_SYNC_GEMINI_API_KEY)",
        },
        {
            "id": "cloudflare-r2",
            "provider": "Cloudflare",
            "name": "Cloudflare R2 Artifact Storage",
            "status": "active" if (settings.cf_r2_bucket or settings.s3_bucket) else "standby",
            "detail": f"Bucket: {settings.cf_r2_bucket or settings.s3_bucket}" if (settings.cf_r2_bucket or settings.s3_bucket) else "Using local durable SQLite-indexed storage",
        },
        {
            "id": "cloudflare-workers-ai",
            "provider": "Cloudflare",
            "name": "Cloudflare Workers AI Edge Fallback",
            "status": "active" if (settings.cf_account_id and settings.cf_api_token) else "standby",
            "detail": f"Model: {settings.cf_workers_ai_model}" if (settings.cf_account_id and settings.cf_api_token) else "Set NEON_SYNC_CF_ACCOUNT_ID + TOKEN",
        },
        {
            "id": "cloudflare-turnstile",
            "provider": "Cloudflare",
            "name": "Cloudflare Turnstile Bot Shield",
            "status": "active" if settings.cf_turnstile_secret else "standby",
            "detail": "Fail-closed siteverify enforced" if settings.cf_turnstile_secret else "Optional bot shield (set NEON_SYNC_CF_TURNSTILE_SECRET)",
        },
    ]
