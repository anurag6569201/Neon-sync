"""Neon Sync Structured API Logging, Request Telemetry & Live Developer Console Stream."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from neon_sync import __version__

from .branding import PlatformMetrics, sanitize_public_text

_SENSITIVE_HEADER_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-neon-pro-key",
        "x-gemini-api-key",
        "x-api-key",
        "proxy-authorization",
    }
)
_SENSITIVE_BODY_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "current_password",
        "new_password",
        "credential",
        "pro_key",
        "gemini_api_key",
    }
)


class ApiLogEntry(BaseModel):
    """Single structured Neon Sync API or pipeline log entry."""

    id: str = Field(default_factory=lambda: f"ns_log_{uuid.uuid4().hex[:12]}")
    request_id: str = Field(default_factory=lambda: f"ns_req_{uuid.uuid4().hex[:12]}")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    level: Literal["INFO", "WARN", "ERROR"] = "INFO"
    service: Literal[
        "neon-sync-api",
        "neon-sync-core",
        "neon-sync-media",
        "neon-sync-ai",
        "neon-sync-guard",
    ] = "neon-sync-api"
    event: str = "api.request"
    method: str | None = None
    path: str | None = None
    status_code: int | None = None
    duration_ms: float | None = None
    client: str | None = None
    tier: str | None = None
    job_id: str | None = None
    summary: str = ""
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: Any = None
    response_preview: Any = None


class LogsPageResponse(BaseModel):
    """Response payload for `GET /v1/logs`."""

    service: str = "neon-sync"
    version: str = __version__
    total_buffered: int
    returned: int
    metrics: PlatformMetrics
    logs: list[ApiLogEntry]


class _NeonSyncConsoleFormatter(logging.Formatter):
    """Clean, professional Neon Sync console log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        lvl = f"{record.levelname:<5}"
        msg = sanitize_public_text(record.getMessage())
        return f"[{ts}] [NEON-SYNC] [{lvl}] {msg}"


def configure_neon_logging() -> logging.Logger:
    """Attach a clean, branded Neon Sync console logger (idempotent)."""
    logger = logging.getLogger("neon_sync.console")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_NeonSyncConsoleFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def mask_secret(value: str) -> str:
    """Mask sensitive tokens while keeping short recognizable prefix/suffix."""
    s = value.strip()
    if len(s) <= 6:
        return "••••••"
    return f"{s[:4]}••••{s[-4:]}"


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return developer-safe request headers with secrets masked and noise trimmed."""
    keep_interesting = {
        "content-type",
        "accept",
        "user-agent",
        "x-request-id",
        "x-neon-pro-key",
        "x-gemini-api-key",
        "x-forwarded-for",
        "origin",
    }
    clean: dict[str, str] = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in _SENSITIVE_HEADER_KEYS:
            clean[k] = mask_secret(v)
        elif kl in keep_interesting:
            clean[k] = v[:180]
    return clean


def redact_payload(value: Any, *, depth: int = 0) -> Any:
    """Recursively sanitize and truncate request/response JSON previews."""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for idx, (k, v) in enumerate(value.items()):
            if idx >= 30:
                out["_truncated"] = f"+{len(value) - 30} keys"
                break
            if str(k).lower() in _SENSITIVE_BODY_KEYS and isinstance(v, str):
                out[k] = mask_secret(v)
            else:
                out[k] = redact_payload(v, depth=depth + 1)
        return out
    if isinstance(value, list):
        if len(value) > 12:
            return [redact_payload(x, depth=depth + 1) for x in value[:12]] + [
                f"… (+{len(value) - 12} items)"
            ]
        return [redact_payload(x, depth=depth + 1) for x in value]
    if isinstance(value, str):
        sanitized = sanitize_public_text(value)
        return sanitized if len(sanitized) <= 500 else sanitized[:497] + "…"
    return value


def classify_api_event(method: str, path: str, status_code: int) -> tuple[str, Literal["neon-sync-api", "neon-sync-core", "neon-sync-media", "neon-sync-ai", "neon-sync-guard"]]:
    """Derive a clean Stripe/Cloudflare-style event slug and Neon Sync service tag."""
    if status_code == 403:
        return "security.request.blocked", "neon-sync-guard"
    if status_code == 402:
        return "billing.tier.limit_reached", "neon-sync-api"
    if status_code == 429:
        return "guard.rate_limit.exceeded", "neon-sync-guard"

    if path.startswith("/v1/auth"):
        if "/google/" in path:
            return ("auth.google.completed" if status_code < 400 else "auth.google.failed"), "neon-sync-guard"
        if path.endswith("/login"):
            return ("auth.login.succeeded" if status_code < 400 else "auth.login.failed"), "neon-sync-guard"
        if path.endswith("/register"):
            return ("auth.register.succeeded" if status_code < 400 else "auth.register.failed"), "neon-sync-guard"
        if path.endswith("/logout"):
            return "auth.logout.completed", "neon-sync-guard"
        if "/api-keys" in path:
            return "auth.api_keys.updated" if method != "GET" else "auth.api_keys.listed", "neon-sync-guard"
        if "/sessions" in path:
            return "auth.sessions.managed", "neon-sync-guard"
        return "auth.session.checked", "neon-sync-guard"
    if path.startswith("/v1/automations"):
        if path.endswith("/run"):
            return ("automation.run.completed" if status_code < 400 else "automation.run.failed"), "neon-sync-ai"
        return ("automation.rule.updated" if method != "GET" else "automation.rules.listed"), "neon-sync-api"
    if path.startswith("/v1/stream"):
        if path.endswith("/resolve"):
            return ("stream.live.resolved" if status_code < 400 else "stream.live.failed"), "neon-sync-media"
        if path.startswith("/v1/stream/playlist/"):
            return "stream.vlc_playlist.exported", "neon-sync-media"
        return "stream.live.playing", "neon-sync-media"
    if path.startswith("/v1/agent"):
        suffix = "completed" if status_code < 400 else "rejected"
        return f"agent.workflow.{suffix}", "neon-sync-ai"
    if path.startswith("/v1/probe"):
        suffix = "completed" if status_code < 400 else "failed"
        return f"media.probe.{suffix}", "neon-sync-core"
    if path.startswith("/v1/jobs/batch"):
        suffix = "queued" if status_code < 400 else "rejected"
        return f"jobs.batch.{suffix}", "neon-sync-core"
    if path.startswith("/v1/jobs") and path.endswith("/cancel"):
        return "jobs.cancelled", "neon-sync-core"
    if path.startswith("/v1/jobs") and path.endswith("/events"):
        return "jobs.stream.connected", "neon-sync-api"
    if path == "/v1/jobs" and method == "POST":
        suffix = "queued" if status_code < 400 else "rejected"
        return f"jobs.created.{suffix}", "neon-sync-core"
    if path.startswith("/v1/jobs"):
        return "jobs.inspected", "neon-sync-api"
    if path.startswith("/v1/artifacts"):
        suffix = "delivered" if status_code < 400 else "not_found"
        return f"artifacts.{suffix}", "neon-sync-media"
    if path.startswith("/v1/status"):
        return "system.status.checked", "neon-sync-api"
    if path.startswith("/v1/health"):
        return "system.health.checked", "neon-sync-api"
    if path.startswith("/v1/formats"):
        return "catalog.formats.listed", "neon-sync-api"
    if path.startswith("/v1/tier"):
        return "account.entitlements.checked", "neon-sync-api"
    return "api.request.completed" if status_code < 400 else "api.request.error", "neon-sync-api"


class ApiTelemetryHub:
    """In-memory ring buffer, live metrics tracker, and SSE broadcaster for Neon Sync logs."""

    def __init__(self, *, max_entries: int = 300) -> None:
        self._started_at = time.monotonic()
        self._buffer: deque[ApiLogEntry] = deque(maxlen=max_entries)
        self._subscribers: set[asyncio.Queue[ApiLogEntry]] = set()
        self._logger = configure_neon_logging()

        # Live counters
        self._total_requests = 0
        self._success_requests = 0
        self._client_errors = 0
        self._server_errors = 0
        self._latencies: deque[float] = deque(maxlen=500)

    def record(self, entry: ApiLogEntry, *, count_as_http: bool = False) -> ApiLogEntry:
        """Store entry, emit branded stdout console log, and notify SSE subscribers."""
        entry.summary = sanitize_public_text(entry.summary)
        self._buffer.appendleft(entry)

        if count_as_http:
            self._total_requests += 1
            if entry.status_code is not None:
                if entry.status_code < 400:
                    self._success_requests += 1
                elif entry.status_code < 500:
                    self._client_errors += 1
                else:
                    self._server_errors += 1
            if entry.duration_ms is not None:
                self._latencies.append(entry.duration_ms)

        # Emit professional console log line
        parts = [f"req={entry.request_id}", f"svc={entry.service}"]
        if entry.method and entry.path:
            parts.append(f"{entry.method} {entry.path}")
        if entry.status_code is not None:
            parts.append(f"status={entry.status_code}")
        if entry.duration_ms is not None:
            parts.append(f"latency={entry.duration_ms:.1f}ms")
        if entry.job_id:
            parts.append(f"job={entry.job_id}")
        parts.append(f"event={entry.event}")
        if entry.summary:
            parts.append(f"msg=\"{entry.summary}\"")
        line = " · ".join(parts)

        if entry.level == "ERROR":
            self._logger.error("%s", line)
        elif entry.level == "WARN":
            self._logger.warning("%s", line)
        else:
            self._logger.info("%s", line)

        for q in list(self._subscribers):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass
        return entry

    def record_pipeline_event(
        self,
        *,
        job_id: str,
        stage: str,
        status: str,
        message: str,
        level: Literal["INFO", "WARN", "ERROR"] = "INFO",
        duration_ms: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ApiLogEntry:
        """Record an internal pipeline lifecycle event branded purely as Neon Sync."""
        svc: Literal["neon-sync-core", "neon-sync-media"] = (
            "neon-sync-media" if stage == "post_processing" else "neon-sync-core"
        )
        entry = ApiLogEntry(
            request_id=f"ns_job_{job_id.replace('job_', '')[:12]}",
            level=level,
            service=svc,
            event=f"pipeline.{stage}",
            method="PIPELINE",
            path=f"/v1/jobs/{job_id}",
            status_code=200 if status == "completed" else (500 if status == "failed" else 202),
            duration_ms=duration_ms,
            job_id=job_id,
            summary=sanitize_public_text(message) or f"Pipeline stage {stage}",
            response_preview=redact_payload(extra) if extra else None,
        )
        return self.record(entry, count_as_http=False)

    def metrics(self, *, active_jobs: int = 0, recent_jobs: int = 0) -> PlatformMetrics:
        avg_ms = (sum(self._latencies) / len(self._latencies)) if self._latencies else 0.0
        return PlatformMetrics(
            uptime_seconds=round(time.monotonic() - self._started_at, 1),
            total_requests=self._total_requests,
            success_requests=self._success_requests,
            client_errors=self._client_errors,
            server_errors=self._server_errors,
            avg_latency_ms=round(avg_ms, 1),
            active_jobs=active_jobs,
            recent_jobs=recent_jobs,
        )

    def list_logs(
        self,
        *,
        limit: int = 100,
        level: str | None = None,
        status_group: str | None = None,
        service: str | None = None,
        search: str | None = None,
    ) -> list[ApiLogEntry]:
        q = (search or "").strip().lower()
        lvl = (level or "").strip().upper()
        sg = (status_group or "").strip().lower()
        svc = (service or "").strip().lower()

        results: list[ApiLogEntry] = []
        for item in self._buffer:
            if lvl and lvl != "ALL" and item.level != lvl:
                continue
            if svc and svc != "all" and item.service != svc:
                continue
            if sg and sg != "all":
                code = item.status_code or 0
                if sg == "2xx" and not (200 <= code < 300):
                    continue
                if sg == "4xx" and not (400 <= code < 500):
                    continue
                if sg == "5xx" and code < 500:
                    continue
                if sg == "errors" and code < 400 and item.level != "ERROR":
                    continue
                if sg == "pipeline" and item.method != "PIPELINE":
                    continue
            if q:
                hay = " ".join(
                    str(x)
                    for x in (
                        item.request_id,
                        item.event,
                        item.method,
                        item.path,
                        item.service,
                        item.job_id,
                        item.summary,
                        item.status_code,
                    )
                    if x is not None
                ).lower()
                if q not in hay:
                    continue
            results.append(item)
            if len(results) >= limit:
                break
        return results

    def clear(self) -> int:
        count = len(self._buffer)
        self._buffer.clear()
        return count

    @property
    def total_buffered(self) -> int:
        return len(self._buffer)

    async def subscribe(self) -> AsyncIterator[ApiLogEntry]:
        q: asyncio.Queue[ApiLogEntry] = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        try:
            while True:
                entry = await q.get()
                yield entry
        finally:
            self._subscribers.discard(q)
