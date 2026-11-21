"""Tier-1 Large Direct File Downloader via ephemeral aria2c CLI subprocess.

License compliance: aria2 is GPL-2.0. Invoked strictly as an isolated CLI subprocess
with RPC, BitTorrent, and Metalink disabled.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import urlsplit

from neon_sync.config import Settings
from neon_sync.models import (
    DownloadPlan,
    EngineCapability,
    EngineManifest,
    ErrorCode,
    HealthReport,
    InstallationStatus,
    InvocationMode,
    MatchScore,
    NormalizedMediaOutput,
    ProbeOptions,
    SupportedDomains,
)
from neon_sync.security import (
    CancellationToken,
    SubprocessCancelledError,
    SubprocessTimeoutError,
    detect_magic_mime,
    ensure_safe_path,
    find_binary,
    run_safe_subprocess,
    sanitize_filename,
)

from .base import AdapterError, DownloadArtifact, ExtractorAdapter, ProgressCallback, UrlContext
from .direct_http import DIRECT_FILE_EXTENSIONS


class Aria2Adapter(ExtractorAdapter):
    """Multi-connection resumable direct file downloader using ephemeral aria2c CLI."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._version = "unknown"

    @property
    def id(self) -> str:
        return "aria2"

    def _exe(self) -> str:
        return find_binary(self._settings.aria2c_bin) or self._settings.aria2c_bin

    def manifest(self) -> EngineManifest:
        installed = find_binary(self._settings.aria2c_bin) is not None
        return EngineManifest(
            id=self.id,
            name="aria2 Multi-Connection Downloader",
            version=self._version,
            tier=1,
            supported_content_types=["file", "document", "archive", "video", "audio", "image"],
            supported_domains=SupportedDomains(mode="wildcard"),
            protocols=["http", "https"],
            capabilities=[
                EngineCapability.DOWNLOAD_DIRECT_FILE,
                EngineCapability.DOWNLOAD_MEDIA,
            ],
            priority=900,
            installation_status=(
                InstallationStatus.INSTALLED if installed else InstallationStatus.MISSING
            ),
            license="GPL-2.0",
            invocation_mode=InvocationMode.SUBPROCESS,
        )

    def quick_match(self, ctx: UrlContext) -> MatchScore:
        ct = (ctx.head_content_type or "").split(";")[0].strip().lower()
        size = ctx.head_content_length or 0
        ext = Path(urlsplit(ctx.url).path).suffix.lower()

        if ct and ct not in {"text/html", "application/xhtml+xml"}:
            if size >= self._settings.direct_aria2_threshold_bytes:
                return MatchScore(
                    engine_id=self.id,
                    confidence=96,
                    reason=f"Large direct file ({size} bytes) ideal for aria2 multi-connection.",
                    predicted_content_type="file",
                )
            return MatchScore(
                engine_id=self.id,
                confidence=70,
                reason="Direct file supported by aria2.",
                predicted_content_type="file",
            )
        if ext in DIRECT_FILE_EXTENSIONS:
            return MatchScore(
                engine_id=self.id,
                confidence=75,
                reason=f"Direct extension '{ext}' supported by aria2.",
                predicted_content_type="file",
            )
        return MatchScore(engine_id=self.id, confidence=0, reason="Not a direct downloadable file.")

    async def probe(
        self,
        ctx: UrlContext,
        options: ProbeOptions,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        raise AdapterError(
            ErrorCode.ERR_UNSUPPORTED_URL,
            "aria2 is a download-only engine; use direct-http for metadata probing.",
            engine_id=self.id,
        )

    def build_download_argv(self, url: str, output_dir: Path, filename: str) -> list[str]:
        """Build locked-down aria2c argv with RPC, BitTorrent, and Metalink disabled."""
        argv = [
            self._exe(),
            "--enable-rpc=false",
            "--follow-torrent=false",
            "--follow-metalink=false",
            "--allow-overwrite=false",
            "--auto-file-renaming=false",
            "--continue=true",
            "--max-connection-per-server=8",
            "--split=8",
            "--min-split-size=1M",
            "--max-tries=3",
            "--retry-wait=2",
            "--timeout=30",
            "--connect-timeout=15",
            "--summary-interval=1",
            "-d",
            str(output_dir),
            "-o",
            filename,
        ]
        if self._settings.egress_proxy_url:
            argv.extend(["--all-proxy", self._settings.egress_proxy_url])
        argv.extend(["--", url])
        return argv

    async def download(
        self,
        ctx: UrlContext,
        plan: DownloadPlan,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadArtifact:
        if find_binary(self._settings.aria2c_bin) is None:
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                "aria2c binary is not installed.",
                engine_id=self.id,
            )

        plan.output_dir.mkdir(parents=True, exist_ok=True)
        raw_name = ctx.head_filename or Path(urlsplit(ctx.url).path).name or "download.bin"
        display_name = sanitize_filename(raw_name)
        ext = Path(display_name).suffix or ".bin"
        disk_name = f"{uuid.uuid4().hex}{ext}"
        target_path = ensure_safe_path(plan.output_dir, plan.output_dir / disk_name)

        argv = self.build_download_argv(ctx.url, plan.output_dir, disk_name)
        try:
            res = await run_safe_subprocess(
                argv,
                cwd=plan.output_dir,
                timeout=float(plan.timeout_seconds),
                proxy_url=self._settings.egress_proxy_url,
                cancel_token=cancel_token,
            )
        except SubprocessTimeoutError as exc:
            target_path.unlink(missing_ok=True)
            raise AdapterError(
                ErrorCode.ERR_TIMEOUT, str(exc), engine_id=self.id, retryable=True
            ) from exc
        except SubprocessCancelledError as exc:
            target_path.unlink(missing_ok=True)
            raise AdapterError(ErrorCode.ERR_INTERNAL, str(exc), engine_id=self.id) from exc

        if res.returncode != 0 or not target_path.exists():
            target_path.unlink(missing_ok=True)
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                f"aria2c exited with code {res.returncode}: {(res.stderr or res.stdout)[-400:]}",
                engine_id=self.id,
                retryable=True,
            )

        size = target_path.stat().st_size
        if size > plan.max_bytes:
            target_path.unlink(missing_ok=True)
            raise AdapterError(
                ErrorCode.ERR_QUOTA_EXCEEDED,
                f"Downloaded file ({size} bytes) exceeded quota ({plan.max_bytes} bytes).",
                engine_id=self.id,
            )

        magic_mime, is_exec = detect_magic_mime(target_path)
        if is_exec:
            target_path.unlink(missing_ok=True)
            raise AdapterError(
                ErrorCode.ERR_VALIDATION_FAILED,
                f"Blocked dangerous executable payload ({magic_mime}).",
                engine_id=self.id,
            )

        return DownloadArtifact(
            local_path=target_path,
            filename=display_name,
            mime_type=magic_mime or ctx.head_content_type or "application/octet-stream",
            size_bytes=size,
        )

    async def health_check(self) -> HealthReport:
        exe = find_binary(self._settings.aria2c_bin)
        if not exe:
            return HealthReport(
                engine_id=self.id,
                installation_status=InstallationStatus.MISSING,
                version="missing",
                healthy=False,
                detail="aria2c not found on PATH.",
            )
        res = await run_safe_subprocess([exe, "--version"], timeout=5.0)
        first_line = (res.stdout.splitlines() or ["unknown"])[0]
        version = first_line.replace("aria2 version", "").strip() or "unknown"
        self._version = version
        return HealthReport(
            engine_id=self.id,
            installation_status=InstallationStatus.INSTALLED,
            version=version,
            healthy=(res.returncode == 0),
            detail=first_line,
        )
