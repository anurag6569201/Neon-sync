"""Job lifecycle, API request payloads, and streaming progress events."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from .media import NormalizedMediaOutput


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    POST_PROCESSING = "post_processing"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProbeRequest(BaseModel):
    url: str = Field(min_length=4, max_length=4096)
    preferred_engine: str | None = None
    max_items: int = Field(default=50, ge=1, le=200)
    include_subtitles: bool = True
    bypass_cache: bool = False


class DownloadRequest(BaseModel):
    url: str = Field(min_length=4, max_length=4096)
    mode: Literal["auto", "video", "audio", "image", "direct"] = "auto"
    format_id: str | None = None
    audio_format: Literal["best", "mp3", "m4a", "opus", "flac", "wav"] | None = None
    preferred_engine: str | None = None
    extract_thumbnail: bool = False
    extract_subtitles: bool = False
    clip_start_seconds: float | None = Field(default=None, ge=0)
    clip_end_seconds: float | None = Field(default=None, ge=0)
    max_items: int = Field(default=50, ge=1, le=200)
    idempotency_key: str | None = Field(default=None, max_length=128)


class ProgressEvent(BaseModel):
    job_id: str
    status: JobStatus
    stage: str
    percent: float | None = Field(default=None, ge=0.0, le=100.0)
    bytes_downloaded: int | None = Field(default=None, ge=0)
    bytes_total: int | None = Field(default=None, ge=0)
    speed_bps: float | None = Field(default=None, ge=0.0)
    eta_seconds: float | None = Field(default=None, ge=0.0)
    items_completed: int | None = Field(default=None, ge=0)
    items_total: int | None = Field(default=None, ge=0)
    message: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JobRecord(BaseModel):
    job_id: str
    url: str
    request: DownloadRequest
    status: JobStatus = JobStatus.QUEUED
    progress: ProgressEvent | None = None
    result: NormalizedMediaOutput | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
