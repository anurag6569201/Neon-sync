"""Abstract interfaces for Engine Registry extractor and post-processor adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neon_sync.models import (
    DownloadPlan,
    EngineManifest,
    ErrorCode,
    HealthReport,
    MatchScore,
    NormalizedMediaOutput,
    ProbeOptions,
    ProgressEvent,
    WarningInfo,
)
from neon_sync.security import CancellationToken, ValidatedUrl

ProgressCallback = Callable[[ProgressEvent], Awaitable[None] | None]


class AdapterError(RuntimeError):
    """Structured error raised by an engine adapter."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        engine_id: str,
        retryable: bool = False,
        outdated: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.engine_id = engine_id
        self.retryable = retryable
        self.outdated = outdated


@dataclass
class UrlContext:
    """Shared context passed through routing, probing, and downloading."""

    validated: ValidatedUrl
    job_id: str
    mode: str = "auto"
    head_status: int | None = None
    head_content_type: str | None = None
    head_content_length: int | None = None
    head_final_url: str | None = None
    head_filename: str | None = None
    prefetched_html: str | None = None

    @property
    def url(self) -> str:
        return self.head_final_url or self.validated.canonical_url

    @property
    def domain(self) -> str:
        return self.validated.domain

    @property
    def path(self) -> str:
        return self.validated.path


@dataclass
class DownloadArtifact:
    """Local scratch artifact produced by an extractor download()."""

    local_path: Path
    filename: str
    mime_type: str | None
    size_bytes: int
    extra_files: list[Path] = field(default_factory=list)
    warnings: list[WarningInfo] = field(default_factory=list)


class ExtractorAdapter(ABC):
    """Common engine interface isolating orchestrator from tool internals."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Unique engine ID matching manifest.id."""

    @abstractmethod
    def manifest(self) -> EngineManifest:
        """Return static capabilities + current installation/health state."""

    @abstractmethod
    def quick_match(self, ctx: UrlContext) -> MatchScore:
        """Zero-network routing confidence (<1ms)."""

    @abstractmethod
    async def probe(
        self,
        ctx: UrlContext,
        options: ProbeOptions,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        """Extract metadata & available formats without downloading media."""

    @abstractmethod
    async def download(
        self,
        ctx: UrlContext,
        plan: DownloadPlan,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadArtifact:
        """Download media into plan.output_dir honoring quotas and cancellation."""

    @abstractmethod
    async def health_check(self) -> HealthReport:
        """Probe binary/runtime installation & version."""


class PostProcessorAdapter(ABC):
    """Common interface for zero-egress media validation and transformation."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Unique post-processor ID."""

    @abstractmethod
    def manifest(self) -> EngineManifest:
        """Return post-processor capability manifest."""

    @abstractmethod
    async def probe_file(self, path: Path) -> dict[str, Any]:
        """Inspect media container/streams locally without network access."""

    @abstractmethod
    async def health_check(self) -> HealthReport:
        """Probe binary availability & version."""
