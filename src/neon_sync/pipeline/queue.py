"""Async Job Queue Manager with durable JobStore, bounded concurrency, SSE pub/sub & rate limiting."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from neon_sync.adapters import AdapterError
from neon_sync.config import Settings
from neon_sync.models import DownloadRequest, JobRecord, JobStatus, ProgressEvent
from neon_sync.security import CancellationToken, SubprocessCancelledError, UrlValidationError

from .job_store import InMemoryJobStore, JobStore
from .orchestrator import ExtractionOrchestrator

log = logging.getLogger("neon_sync.queue")


class RateLimitExceededError(RuntimeError):
    """Raised when a caller exceeds their sliding-window request budget."""


class SlidingWindowRateLimiter:
    """Per-client sliding-window rate limiter (Redis-swappable)."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check_and_consume(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        bucket = self._hits[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_requests:
            raise RateLimitExceededError(
                f"Rate limit exceeded: max {self.max_requests} requests per {int(self.window_seconds)}s."
            )
        bucket.append(now)


class JobManager:
    """Manages asynchronous download jobs, durable persistence, progress broadcasts & cancellation."""

    def __init__(
        self,
        settings: Settings,
        orchestrator: ExtractionOrchestrator,
        *,
        store: JobStore | None = None,
    ) -> None:
        self._settings = settings
        self._orchestrator = orchestrator
        self._store: JobStore = store or InMemoryJobStore()
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._active: dict[str, JobRecord] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._subscribers: dict[str, set[asyncio.Queue[ProgressEvent | None]]] = defaultdict(set)

    def get_job(self, job_id: str) -> JobRecord | None:
        return self._active.get(job_id) or self._store.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        return self._store.list_recent(limit=limit)

    async def _broadcast(self, job_id: str, event: ProgressEvent | None) -> None:
        for q in list(self._subscribers.get(job_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def submit(self, request: DownloadRequest) -> JobRecord:
        if request.idempotency_key:
            existing_id = self._store.get_idempotency(request.idempotency_key)
            if existing_id:
                existing = self.get_job(existing_id)
                if existing is not None:
                    return existing

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

        async def _on_progress(event: ProgressEvent) -> None:
            record.status = event.status
            record.progress = event
            record.updated_at = datetime.now(UTC)
            self._store.save(record)
            await self._broadcast(job_id, event)

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
                    message="Download complete",
                )
                record.progress = done_evt
                self._store.save(record)
                await self._broadcast(job_id, done_evt)
        except (SubprocessCancelledError, asyncio.CancelledError):
            now = datetime.now(UTC)
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
        except (UrlValidationError, AdapterError, Exception) as exc:  # noqa: BLE001
            log.warning("Job %s failed: %s", job_id, exc)
            now = datetime.now(UTC)
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
        return self.get_job(job_id)

    async def subscribe(self, job_id: str) -> AsyncIterator[ProgressEvent]:
        record = self.get_job(job_id)
        if record is None:
            return
        if record.progress is not None:
            yield record.progress
        if record.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return

        q: asyncio.Queue[ProgressEvent | None] = asyncio.Queue(maxsize=100)
        self._subscribers[job_id].add(q)
        try:
            while True:
                event = await q.get()
                if event is None:
                    break
                yield event
        finally:
            self._subscribers[job_id].discard(q)

    async def shutdown(self) -> None:
        for job_id in list(self._tasks.keys()):
            await self.cancel(job_id, reason="Server shutting down")
