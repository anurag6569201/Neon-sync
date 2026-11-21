"""SaaS Tier & Freemium Credit Engine (Zero-Signup Guest Trial vs Pro Entitlements)."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Literal

from pydantic import BaseModel

from neon_sync.config import Settings
from neon_sync.models import DownloadRequest

TierName = Literal["guest", "pro"]


class TierEntitlements(BaseModel):
    tier: TierName
    downloads_limit_hourly: int
    downloads_remaining: int
    agent_runs_limit_daily: int
    agent_runs_remaining: int
    max_video_quality: str
    max_job_bytes: int
    max_batch_urls: int
    allow_lossless_audio: bool
    allow_4k: bool
    allow_browser_fallback: bool
    has_llm_provider: bool


class TierLimitError(RuntimeError):
    """Raised when a Guest user hits a Freemium cap or Pro-only feature."""

    def __init__(self, code: str, message: str, *, upgrade_hint: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.upgrade_hint = upgrade_hint


class TierManager:
    """Tracks per-client Guest trial credits and validates Pro entitlements."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pro_keys = {
            k.strip() for k in settings.pro_api_keys.split(",") if k.strip()
        }
        self._dl_hits: dict[str, deque[float]] = defaultdict(deque)
        self._agent_hits: dict[str, deque[float]] = defaultdict(deque)

    def resolve_tier(self, pro_key: str | None, byok_key: str | None = None) -> TierName:
        if pro_key and pro_key.strip() in self._pro_keys:
            return "pro"
        # Users bringing their own LLM key unlock Pro agent runs cleanly
        if byok_key and byok_key.strip():
            return "pro"
        return "guest"

    def _prune(self, bucket: deque[float], window_seconds: float) -> None:
        cutoff = time.monotonic() - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def get_entitlements(
        self,
        client_key: str,
        *,
        pro_key: str | None = None,
        byok_key: str | None = None,
    ) -> TierEntitlements:
        tier = self.resolve_tier(pro_key, byok_key)
        has_llm = bool(
            byok_key
            or self._settings.gemini_api_key
            or self._settings.openai_api_key
            or self._settings.ollama_url
        )

        if tier == "pro":
            return TierEntitlements(
                tier="pro",
                downloads_limit_hourly=500,
                downloads_remaining=500,
                agent_runs_limit_daily=500,
                agent_runs_remaining=500,
                max_video_quality="2160",
                max_job_bytes=self._settings.max_job_bytes,
                max_batch_urls=10,
                allow_lossless_audio=True,
                allow_4k=True,
                allow_browser_fallback=True,
                has_llm_provider=has_llm,
            )

        dl_bucket = self._dl_hits[client_key]
        ag_bucket = self._agent_hits[client_key]
        self._prune(dl_bucket, 3600.0)
        self._prune(ag_bucket, 86400.0)

        dl_rem = max(0, self._settings.guest_downloads_per_hour - len(dl_bucket))
        ag_rem = max(0, self._settings.guest_agent_runs_per_day - len(ag_bucket))
        return TierEntitlements(
            tier="guest",
            downloads_limit_hourly=self._settings.guest_downloads_per_hour,
            downloads_remaining=dl_rem,
            agent_runs_limit_daily=self._settings.guest_agent_runs_per_day,
            agent_runs_remaining=ag_rem,
            max_video_quality=str(self._settings.guest_max_video_height),
            max_job_bytes=self._settings.guest_max_job_bytes,
            max_batch_urls=self._settings.guest_max_batch_urls,
            allow_lossless_audio=False,
            allow_4k=False,
            allow_browser_fallback=False,
            has_llm_provider=has_llm,
        )

    def check_and_consume_download(
        self,
        client_key: str,
        request: DownloadRequest,
        *,
        pro_key: str | None = None,
        byok_key: str | None = None,
    ) -> TierEntitlements:
        ent = self.get_entitlements(client_key, pro_key=pro_key, byok_key=byok_key)
        if not self._settings.enforce_saas_tiers or ent.tier == "pro":
            return ent

        if request.video_quality in {"2160", "1440"} and not ent.allow_4k:
            raise TierLimitError(
                "ERR_PRO_FEATURE_REQUIRED",
                f"{request.video_quality}p Ultra-HD extraction is a Pro feature (Guest includes up to 1080p).",
                upgrade_hint="Unlock Pro or select 1080p / Best for instant free download.",
            )
        if request.audio_format in {"flac", "wav"} and not ent.allow_lossless_audio:
            raise TierLimitError(
                "ERR_PRO_FEATURE_REQUIRED",
                f"Lossless {request.audio_format.upper()} mastering is a Pro feature (Guest includes MP3, M4A & Opus).",
                upgrade_hint="Unlock Pro or select MP3 320k / M4A.",
            )
        if request.allow_browser_fallback and not ent.allow_browser_fallback:
            raise TierLimitError(
                "ERR_PRO_FEATURE_REQUIRED",
                "Tier-3 Playwright headless browser extraction is a Pro feature.",
                upgrade_hint="Unlock Pro to enable JS-rendered browser extraction.",
            )
        if ent.downloads_remaining <= 0:
            raise TierLimitError(
                "ERR_GUEST_QUOTA_EXCEEDED",
                f"Guest limit reached ({ent.downloads_limit_hourly} free downloads/hour).",
                upgrade_hint="Unlock Pro for 500 downloads/hour, 4K, Lossless FLAC & Batch 10x.",
            )

        self._dl_hits[client_key].append(time.monotonic())
        return self.get_entitlements(client_key, pro_key=pro_key, byok_key=byok_key)

    def check_and_consume_agent(
        self,
        client_key: str,
        *,
        pro_key: str | None = None,
        byok_key: str | None = None,
    ) -> TierEntitlements:
        ent = self.get_entitlements(client_key, pro_key=pro_key, byok_key=byok_key)
        if not self._settings.enforce_saas_tiers or ent.tier == "pro":
            return ent

        if ent.agent_runs_remaining <= 0:
            raise TierLimitError(
                "ERR_AGENT_QUOTA_EXCEEDED",
                f"You've used all {ent.agent_runs_limit_daily} free Guest AI Agent runs for today.",
                upgrade_hint="Unlock Pro or add your own Gemini/OpenAI API key (BYOK) for unlimited Agent runs.",
            )

        self._agent_hits[client_key].append(time.monotonic())
        return self.get_entitlements(client_key, pro_key=pro_key, byok_key=byok_key)
