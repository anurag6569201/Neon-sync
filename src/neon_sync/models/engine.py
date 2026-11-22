"""Universal Engine Registry manifests and adapter execution contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .options import AdvancedProcessingOptions


class EngineCapability(StrEnum):
    EXTRACT_METADATA = "extract_metadata"
    ENUMERATE_FORMATS = "enumerate_formats"
    EXTRACT_MEDIA_URL = "extract_media_url"
    DOWNLOAD_MEDIA = "download_media"
    DOWNLOAD_DIRECT_FILE = "download_direct_file"
    DOWNLOAD_GALLERY = "download_gallery"
    DOWNLOAD_PLAYLIST = "download_playlist"
    EXTRACT_SUBTITLES = "extract_subtitles"
    EXTRACT_THUMBNAIL = "extract_thumbnail"
    EXTRACT_AUDIO = "extract_audio"
    PROCESS_MEDIA = "process_media"
    RECORD_LIVESTREAM = "record_livestream"
    RENDER_JAVASCRIPT = "render_javascript"
    SUPPORT_AUTHENTICATED_CONTENT = "support_authenticated_content"


class InstallationStatus(StrEnum):
    INSTALLED = "installed"
    MISSING = "missing"
    MISCONFIGURED = "misconfigured"


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CIRCUIT_OPEN = "circuit_open"
    UNAVAILABLE = "unavailable"


class InvocationMode(StrEnum):
    SUBPROCESS = "subprocess"
    HTTP_SIDECAR = "http_sidecar"
    IN_PROCESS = "in_process"


class SupportedDomains(BaseModel):
    mode: Literal["allowlist", "pattern_list", "wildcard"] = "wildcard"
    domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    site_count: int | None = Field(default=None, ge=0)


class AuthRequirements(BaseModel):
    supports_cookies: bool = False
    supports_oauth: bool = False
    supports_headers: bool = False
    required_for_domains: list[str] = Field(default_factory=list)


class CostProfile(BaseModel):
    avg_probe_ms: int = Field(default=500, ge=0)
    memory_mb: int = Field(default=64, ge=1)
    requires_browser: bool = False


class HealthStatus(BaseModel):
    state: HealthState = HealthState.HEALTHY
    last_checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    consecutive_failures: int = Field(default=0, ge=0)
    error_rate_5m: float = Field(default=0.0, ge=0.0, le=1.0)
    outdated_detected: bool = False


class EngineManifest(BaseModel):
    """Matches research/schemas/engine-manifest.schema.json."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,31}$")
    name: str
    version: str
    tier: Literal[1, 2, 3]
    supported_content_types: list[str] = Field(min_length=1)
    supported_domains: SupportedDomains
    protocols: list[str] = Field(default_factory=lambda: ["http", "https"])
    capabilities: list[EngineCapability] = Field(min_length=1)
    auth_requirements: AuthRequirements = Field(default_factory=AuthRequirements)
    cost_profile: CostProfile = Field(default_factory=CostProfile)
    priority: int = Field(ge=0, le=1000)
    installation_status: InstallationStatus = InstallationStatus.INSTALLED
    health_status: HealthStatus = Field(default_factory=HealthStatus)
    license: str
    invocation_mode: InvocationMode


class MatchScore(BaseModel):
    """Zero-network routing confidence emitted by an adapter's quick_match()."""

    engine_id: str
    confidence: int = Field(ge=0, le=100)
    reason: str
    predicted_content_type: str | None = None


class HealthReport(BaseModel):
    engine_id: str
    installation_status: InstallationStatus
    version: str
    healthy: bool
    detail: str = ""
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProbeOptions(BaseModel):
    timeout_seconds: int = Field(default=25, ge=1, le=120)
    max_items: int = Field(default=50, ge=1, le=500)
    include_subtitles: bool = True
    preferred_engine: str | None = None
    allow_browser_fallback: bool = False


class DownloadPlan(AdvancedProcessingOptions):
    job_id: str
    output_dir: Path
    format_id: str | None = None
    mode: Literal["auto", "video", "audio", "image", "direct", "webpage"] = "auto"
    max_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)
    max_items: int = Field(default=50, ge=1, le=500)
    timeout_seconds: int = Field(default=1800, ge=10, le=7200)
