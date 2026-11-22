"""Cloudflare Turnstile Siteverify helper for Neon Sync."""

from __future__ import annotations

import logging

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
