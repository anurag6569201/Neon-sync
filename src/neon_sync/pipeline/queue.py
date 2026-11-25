"""Async Job Queue Manager with durable JobStore, bounded concurrency, SSE pub/sub & rate limiting."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from neon_sync.adapters import AdapterError
from neon_sync.config import Settings
from neon_sync.models import DownloadRequest, JobRecord, JobStatus, ProgressEvent
from neon_sync.security import CancellationToken, SubprocessCancelledError, UrlValidationError

from .job_store import TERMINAL_STATUSES, InMemoryJobStore, JobStore
from .orchestrator import ExtractionOrchestrator

log = logging.getLogger("neon_sync.queue")


class RateLimitExceededError(RuntimeError):
    """Raised when a caller exceeds their sliding-window request budget."""


class QueueCapacityError(RuntimeError):
    """Raised before submission when the bounded queue is full."""


class SlidingWindowRateLimiter:
    """Per-client sliding-window rate limiter (Redis-swappable)."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check_and_consume(self, key: str, count: int = 1) -> None:
        if count < 1:
            raise ValueError("count must be positive")
        now = time.monotonic()
        cutoff = now - self.window_seconds
        bucket = self._hits[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) + count > self.max_requests:
            raise RateLimitExceededError(
                f"Rate limit exceeded: max {self.max_requests} requests per {int(self.window_seconds)}s."
            )
        bucket.extend([now] * count)


class JobManager:
    """Manages asynchronous download jobs, durable persistence, progress broadcasts & cancellation."""

    def __init__(
        self,
        settings: Settings,
        orchestrator: ExtractionOrchestrator,
        *,
        store: JobStore | None = None,
        on_pipeline_event: Callable[..., Any] | None = None,
    ) -> None:
        self._settings = settings
        self._orchestrator = orchestrator
        self._store: JobStore = store or InMemoryJobStore()
        self._on_pipeline_event = on_pipeline_event
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._active: dict[str, JobRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._subscribers: dict[str, set[asyncio.Queue[ProgressEvent | None]]] = defaultdict(set)

    @property
    def active_count(self) -> int:
        return len(self._active)

    def _emit_pipeline_log(
        self,
        *,
        job_id: str,
        stage: str,
        status: str,
        message: str,
        level: str = "INFO",
        duration_ms: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self._on_pipeline_event is None:
            return
        try:
            self._on_pipeline_event(
                job_id=job_id,
                stage=stage,
                status=status,
                message=message,
                level=level,
                duration_ms=duration_ms,
                extra=extra,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Pipeline telemetry callback ignored error: %s", exc)

    def get_job(self, job_id: str) -> JobRecord | None:
        return self._active.get(job_id) or self._store.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        return self._store.list_recent(limit=limit)

    def ensure_capacity(self, count: int = 1) -> None:
        if len(self._active) + count > self._settings.max_pending_jobs:
            raise QueueCapacityError("The download queue is full. Try again after a job finishes.")

    def find_idempotent_job(self, request: DownloadRequest) -> JobRecord | None:
        if request.idempotency_key:
            job_id = self._store.get_idempotency(request.idempotency_key)
            if job_id:
                return self.get_job(job_id)
        return None

    def recover_interrupted_jobs(self) -> int:
        """Finish stale records after restart; do not silently repeat downloads."""
        recovered = 0
        for record in self._store.list_unfinished():
            if record.job_id in self._active:
                continue
            now = datetime.now(UTC)
            record.status = JobStatus.FAILED
            record.error = "Server restarted before this job finished. Retry the download."
            record.updated_at = now
            record.completed_at = now
            record.progress = ProgressEvent(
                job_id=record.job_id,
                status=JobStatus.FAILED,
                stage="interrupted",
                message=record.error,
            )
            self._store.save(record)
            recovered += 1
        return recovered

    async def _broadcast(self, job_id: str, event: ProgressEvent | None) -> None:
        for q in list(self._subscribers.get(job_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow subscribers need the latest state, especially completion.
                q.get_nowait()
                q.put_nowait(event)

    async def submit(self, request: DownloadRequest) -> JobRecord:
        existing = self.find_idempotent_job(request)
        if existing is not None:
            return existing

        self.ensure_capacity()

        job_id = f"job_{uuid.uuid4().hex[:16]}"
        record = JobRecord(
            job_id=job_id,
            url=request.url,
            request=request,
            status=JobStatus.QUEUED,
            progress=ProgressEvent(
                job_id=job_id,
                status=JobStatus.QUEUED,
                stage="queued",
                percent=0.0,
                message="Job queued",
            ),
        )
        self._active[job_id] = record
        self._store.save(record)
        if request.idempotency_key:
            self._store.set_idempotency(request.idempotency_key, job_id)

        token = CancellationToken()
        self._tokens[job_id] = token

        task = asyncio.create_task(self._run_job(job_id, request, token), name=f"job:{job_id}")
        self._tasks[job_id] = task
        return record

    async def _run_job(
        self,
        job_id: str,
        request: DownloadRequest,
        token: CancellationToken,
    ) -> None:
        record = self._active[job_id]
        started = time.monotonic()
        last_stage: str | None = None

        async def _on_progress(event: ProgressEvent) -> None:
            nonlocal last_stage
            record.status = event.status
            record.progress = event
            record.updated_at = datetime.now(UTC)
            self._store.save(record)
            await self._broadcast(job_id, event)
            if event.stage != last_stage:
                last_stage = event.stage
                self._emit_pipeline_log(
                    job_id=job_id,
                    stage=event.stage,
                    status=event.status.value,
                    message=event.message or f"Stage: {event.stage}",
                    extra={"percent": event.percent, "mode": request.mode},
                )

        try:
            async with self._semaphore:
                if token.is_cancelled:
                    raise SubprocessCancelledError(token.reason or "cancelled before start")

                result = await self._orchestrator.execute_download(
                    job_id,
                    request,
                    progress_cb=_on_progress,
                    cancel_token=token,
                )
                now = datetime.now(UTC)
                elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
                record.status = JobStatus.COMPLETED
                record.result = result
                record.completed_at = now
                record.updated_at = now
                done_evt = ProgressEvent(
                    job_id=job_id,
                    status=JobStatus.COMPLETED,
                    stage="completed",
                    percent=100.0,
                    bytes_downloaded=result.download.size_bytes,
                    bytes_total=result.download.size_bytes,
                    message="Extraction complete",
                )
                record.progress = done_evt
                self._store.save(record)
                await self._broadcast(job_id, done_evt)
                self._emit_pipeline_log(
                    job_id=job_id,
                    stage="completed",
                    status="completed",
                    message=f"Artifact ready: {result.download.filename} ({result.download.size_bytes or 0:,} bytes)",
                    duration_ms=elapsed_ms,
                    extra={
                        "filename": result.download.filename,
                        "size_bytes": result.download.size_bytes,
                        "mime_type": result.download.mime_type,
                    },
                )
        except (SubprocessCancelledError, asyncio.CancelledError):
            now = datetime.now(UTC)
            elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
            record.status = JobStatus.CANCELLED
            record.error = token.reason or "Job cancelled"
            record.completed_at = now
            record.updated_at = now
            evt = ProgressEvent(
                job_id=job_id,
                status=JobStatus.CANCELLED,
                stage="cancelled",
                message=record.error,
            )
            record.progress = evt
            self._store.save(record)
            await self._broadcast(job_id, evt)
            self._emit_pipeline_log(
                job_id=job_id,
                stage="cancelled",
                status="cancelled",
                message=record.error,
                level="WARN",
                duration_ms=elapsed_ms,
            )
        except (UrlValidationError, AdapterError, Exception) as exc:  # noqa: BLE001
            log.warning("Job %s failed: %s", job_id, exc)
            now = datetime.now(UTC)
            elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
            record.status = JobStatus.FAILED
            record.error = str(exc)
            record.result = self._orchestrator.build_error_output(
                job_id=job_id,
                raw_url=request.url,
                exc=exc,
            )
            record.completed_at = now
            record.updated_at = now
            evt = ProgressEvent(
                job_id=job_id,
                status=JobStatus.FAILED,
                stage="failed",
                message=str(exc),
            )
            record.progress = evt
            self._store.save(record)
            await self._broadcast(job_id, evt)
            self._emit_pipeline_log(
                job_id=job_id,
                stage="failed",
                status="failed",
                message=str(exc),
                level="ERROR",
                duration_ms=elapsed_ms,
            )
        finally:
            await self._broadcast(job_id, None)
            self._tasks.pop(job_id, None)
            self._tokens.pop(job_id, None)
            self._active.pop(job_id, None)

    async def cancel(self, job_id: str, reason: str = "Cancelled by user") -> JobRecord | None:
        record = self.get_job(job_id)
        if record is None:
            return None
        if record.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return record
        token = self._tokens.get(job_id)
        if token is not None:
            token.cancel(reason)
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            # A task cancelled before its first event-loop turn never enters
            # _run_job's try/finally, so finish and release it here.
            if task.done() and record.status not in TERMINAL_STATUSES:
                now = datetime.now(UTC)
                record.status = JobStatus.CANCELLED
                record.error = reason
                record.updated_at = now
                record.completed_at = now
                record.progress = ProgressEvent(
                    job_id=job_id, status=JobStatus.CANCELLED, stage="cancelled", message=reason,
                )
                self._store.save(record)
                await self._broadcast(job_id, record.progress)
                await self._broadcast(job_id, None)
                self._tasks.pop(job_id, None)
                self._tokens.pop(job_id, None)
                self._active.pop(job_id, None)
        return self.get_job(job_id)

    async def subscribe(self, job_id: str) -> AsyncIterator[ProgressEvent]:
        q: asyncio.Queue[ProgressEvent | None] = asyncio.Queue(maxsize=100)
        # Register before yielding the snapshot: a job may finish while the
        # consumer is handling that first event.
        self._subscribers[job_id].add(q)
        try:
            record = self.get_job(job_id)
            if record is None:
                return
            terminal = record.status in TERMINAL_STATUSES
            if record.progress is not None:
                yield record.progress
            if terminal:
                return
            while True:
                event = await q.get()
                if event is None:
                    break
                yield event
        finally:
            self._subscribers[job_id].discard(q)
            if not self._subscribers[job_id]:
                self._subscribers.pop(job_id, None)

    async def shutdown(self) -> None:
        for job_id in list(self._tasks.keys()):
            await self.cancel(job_id, reason="Server shutting down")
