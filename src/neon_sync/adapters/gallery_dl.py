"""Tier-2 Image Gallery & Multi-Photo Social Extractor via isolated gallery-dl CLI subprocess.

License compliance: mikf/gallery-dl is GPL-2.0. It is NEVER imported into Python;
it is invoked strictly across a CLI subprocess boundary with `--ignore-config` and
strict `--range` item caps to prevent unbounded profile/gallery disk exhaustion.
"""

from __future__ import annotations

import json
import mimetypes
import time
import zipfile
from pathlib import Path

from neon_sync.config import Settings
from neon_sync.models import (
    AuthorInfo,
    AuthRequirements,
    ContentType,
    CostProfile,
    DownloadInfo,
    DownloadPlan,
    DownloadStatus,
    EngineCapability,
    EngineManifest,
    EngineProvenance,
    ErrorCode,
    FormatInfo,
    HealthReport,
    InstallationStatus,
    InvocationMode,
    MatchScore,
    MediaItem,
    MediaMetadata,
    NormalizedMediaOutput,
    ProbeOptions,
    ProtocolType,
    SourceInfo,
    SupportedDomains,
    ThumbnailInfo,
)
from neon_sync.security import (
    CancellationToken,
    SubprocessCancelledError,
    SubprocessTimeoutError,
    detect_magic_mime,
    find_binary,
    run_safe_subprocess,
    sanitize_filename,
)

from .base import AdapterError, DownloadArtifact, ExtractorAdapter, ProgressCallback, UrlContext

GALLERY_PRIMARY_DOMAINS: frozenset[str] = frozenset(
    {
        "imgur.com",
        "pixiv.net",
        "deviantart.com",
        "flickr.com",
        "artstation.com",
        "pinterest.com",
        "pin.it",
        "tumblr.com",
        "danbooru.donmai.us",
        "gelbooru.com",
        "mangadex.org",
        "behance.net",
        "500px.com",
        "unsplash.com",
    }
)

HYBRID_SOCIAL_DOMAINS: frozenset[str] = frozenset(
    {
        "instagram.com",
        "x.com",
        "twitter.com",
        "reddit.com",
        "bsky.app",
        "bluesky.app",
    }
)


def parse_gallery_dl_json(
    raw_output: str,
    ctx: UrlContext,
    *,
    version: str,
    probe_ms: int,
) -> NormalizedMediaOutput:
    """Normalize gallery-dl --dump-json output ([msg_type, ...]) into NormalizedMediaOutput.

    gallery-dl emits a JSON array where each element is:
      [2, {directory_metadata}]
      [3, "https://direct.image.url", {item_metadata}]
    """
    data = json.loads(raw_output.strip())
    if not isinstance(data, list):
        raise TypeError("Expected JSON list from gallery-dl --dump-json")

    dir_meta: dict = {}
    items: list[MediaItem] = []

    for entry in data:
        if not isinstance(entry, list) or not entry:
            continue
        msg_type = entry[0]
        if msg_type == 2 and len(entry) >= 2 and isinstance(entry[1], dict):
            dir_meta.update(entry[1])
        elif msg_type == 3 and len(entry) >= 3 and isinstance(entry[1], str):
            item_url = entry[1]
            meta = entry[2] if isinstance(entry[2], dict) else {}
            ext = str(meta.get("extension") or Path(item_url).suffix.lstrip(".") or "jpg").lower()
            mime = mimetypes.guess_type(f"x.{ext}")[0] or f"image/{ext}"
            kind = "video" if mime.startswith("video/") else "image"
            items.append(
                MediaItem(
                    index=len(items) + 1,
                    external_id=str(meta.get("id") or meta.get("filename") or len(items) + 1),
                    url=item_url,
                    title=meta.get("title") or meta.get("filename"),
                    content_type=kind,
                    mime_type=mime,
                    width=meta.get("width") if isinstance(meta.get("width"), int) else None,
                    height=meta.get("height") if isinstance(meta.get("height"), int) else None,
                    direct_media_url=item_url,
                )
            )

    if not items:
        raise AdapterError(
            ErrorCode.ERR_UNSUPPORTED_URL,
            "gallery-dl found no downloadable media items.",
            engine_id="gallery-dl",
        )

    title = (
        dir_meta.get("title")
        or dir_meta.get("description")
        or items[0].title
        or f"{ctx.domain} gallery"
    )
    author_name = (
        dir_meta.get("author")
        or dir_meta.get("username")
        or dir_meta.get("user", {}).get("name")
        if isinstance(dir_meta.get("user"), dict)
        else dir_meta.get("author") or dir_meta.get("username")
    )
    is_single = len(items) == 1
    first = items[0]
    formats = [
        FormatInfo(
            format_id="gallery-archive" if not is_single else "image-original",
            label=(
                f"Gallery Archive ({len(items)} items, ZIP)"
                if not is_single
                else f"Original Image ({(first.mime_type or 'image').upper()})"
            ),
            container="zip" if not is_single else Path(first.url).suffix.lstrip(".") or "jpg",
            mime_type="application/zip" if not is_single else (first.mime_type or "image/jpeg"),
            width=first.width if is_single else None,
            height=first.height if is_single else None,
            protocol=ProtocolType.HTTPS if first.url.startswith("https://") else ProtocolType.HTTP,
            direct_media_url=first.direct_media_url if is_single else None,
        )
    ]

    return NormalizedMediaOutput(
        job_id=ctx.job_id,
        original_url=ctx.validated.original_url,
        url=ctx.url,
        source=SourceInfo(
            domain=ctx.domain,
            platform=str(dir_meta.get("category") or ctx.domain).lower(),
            extractor_key=str(dir_meta.get("subcategory") or dir_meta.get("category") or "gallery-dl"),
        ),
        content_type=ContentType.IMAGE if is_single else ContentType.GALLERY,
        title=str(title)[:300],
        description=str(dir_meta["description"])[:1000] if isinstance(dir_meta.get("description"), str) else None,
        thumbnail=ThumbnailInfo(url=first.url, width=first.width, height=first.height, mime_type=first.mime_type),
        author=AuthorInfo(name=str(author_name)) if author_name else None,
        available_formats=formats,
        items=items,
        metadata=MediaMetadata(
            item_count_total=len(items),
            item_count_returned=len(items),
        ),
        download=DownloadInfo(status=DownloadStatus.NOT_REQUESTED),
        engine=EngineProvenance(
            engine_used="gallery-dl",
            engine_version=version,
            fallback_chain=["gallery-dl"],
            probe_duration_ms=probe_ms,
        ),
    )


class GalleryDlAdapter(ExtractorAdapter):
    """Primary image gallery and multi-photo social post engine (GPL-2.0 CLI subprocess)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._version = "unknown"

    @property
    def id(self) -> str:
        return "gallery-dl"

    def _exe(self) -> str:
        return find_binary(self._settings.gallery_dl_bin) or self._settings.gallery_dl_bin

    def manifest(self) -> EngineManifest:
        installed = find_binary(self._settings.gallery_dl_bin) is not None
        return EngineManifest(
            id=self.id,
            name="gallery-dl",
            version=self._version,
            tier=2,
            supported_content_types=["image", "gallery"],
            supported_domains=SupportedDomains(
                mode="pattern_list",
                domains=sorted(GALLERY_PRIMARY_DOMAINS | HYBRID_SOCIAL_DOMAINS),
                site_count=300,
            ),
            protocols=["http", "https"],
            capabilities=[
                EngineCapability.EXTRACT_METADATA,
                EngineCapability.EXTRACT_MEDIA_URL,
                EngineCapability.DOWNLOAD_GALLERY,
                EngineCapability.DOWNLOAD_MEDIA,
                EngineCapability.SUPPORT_AUTHENTICATED_CONTENT,
            ],
            auth_requirements=AuthRequirements(
                supports_cookies=True,
                supports_oauth=True,
                supports_headers=True,
                required_for_domains=["pixiv.net", "instagram.com"],
            ),
            cost_profile=CostProfile(avg_probe_ms=1200, memory_mb=96, requires_browser=False),
            priority=880,
            installation_status=(
                InstallationStatus.INSTALLED if installed else InstallationStatus.MISSING
            ),
            license="GPL-2.0",
            invocation_mode=InvocationMode.SUBPROCESS,
        )

    def quick_match(self, ctx: UrlContext) -> MatchScore:
        if ctx.domain in GALLERY_PRIMARY_DOMAINS:
            return MatchScore(
                engine_id=self.id,
                confidence=96,
                reason=f"Primary gallery platform '{ctx.domain}'.",
                predicted_content_type="gallery",
            )
        if ctx.domain in HYBRID_SOCIAL_DOMAINS:
            conf = 92 if ctx.mode == "image" else 72
            return MatchScore(
                engine_id=self.id,
                confidence=conf,
                reason=f"Hybrid social platform '{ctx.domain}' (gallery/photo fallback).",
                predicted_content_type="gallery",
            )
        return MatchScore(engine_id=self.id, confidence=0, reason="Not a known gallery domain.")

    def build_probe_argv(self, url: str, options: ProbeOptions) -> list[str]:
        cap = min(options.max_items, self._settings.max_gallery_items)
        argv = [
            self._exe(),
            "--ignore-config",
            "--dump-json",
            "--range",
            f"1-{cap}",
        ]
        if self._settings.egress_proxy_url:
            argv.extend(["-o", f"proxy={self._settings.egress_proxy_url}"])
        argv.extend(["--", url])
        return argv

    def build_download_argv(self, url: str, plan: DownloadPlan) -> list[str]:
        cap = min(plan.max_items, self._settings.max_gallery_items)
        argv = [
            self._exe(),
            "--ignore-config",
            "--no-mtime",
            "--range",
            f"1-{cap}",
            "-d",
            str(plan.output_dir),
        ]
        if self._settings.egress_proxy_url:
            argv.extend(["-o", f"proxy={self._settings.egress_proxy_url}"])
        argv.extend(["--", url])
        return argv

    async def probe(
        self,
        ctx: UrlContext,
        options: ProbeOptions,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> NormalizedMediaOutput:
        t0 = time.monotonic()
        argv = self.build_probe_argv(ctx.url, options)
        try:
            res = await run_safe_subprocess(
                argv,
                timeout=float(options.timeout_seconds),
                proxy_url=self._settings.egress_proxy_url,
                cancel_token=cancel_token,
            )
        except SubprocessTimeoutError as exc:
            raise AdapterError(
                ErrorCode.ERR_TIMEOUT, str(exc), engine_id=self.id, retryable=True
            ) from exc
        except SubprocessCancelledError as exc:
            raise AdapterError(ErrorCode.ERR_INTERNAL, str(exc), engine_id=self.id) from exc

        if res.returncode != 0:
            err_text = (res.stderr or res.stdout).strip()[-400:]
            low = err_text.lower()
            if "401" in low or "403" in low or "login" in low or "auth" in low:
                raise AdapterError(ErrorCode.ERR_AUTH_REQUIRED, err_text, engine_id=self.id)
            if "404" in low or "not found" in low:
                raise AdapterError(ErrorCode.ERR_NOT_FOUND, err_text, engine_id=self.id)
            if "unsupported" in low or "no suitable extractor" in low:
                raise AdapterError(ErrorCode.ERR_UNSUPPORTED_URL, err_text, engine_id=self.id)
            raise AdapterError(
                ErrorCode.ERR_INTERNAL, err_text, engine_id=self.id, retryable=True
            )

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        try:
            return parse_gallery_dl_json(res.stdout, ctx, version=self._version, probe_ms=elapsed_ms)
        except AdapterError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                f"Failed to parse gallery-dl output: {exc}",
                engine_id=self.id,
            ) from exc

    async def download(
        self,
        ctx: UrlContext,
        plan: DownloadPlan,
        *,
        progress_cb: ProgressCallback | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadArtifact:
        plan.output_dir.mkdir(parents=True, exist_ok=True)
        argv = self.build_download_argv(ctx.url, plan)
        try:
            res = await run_safe_subprocess(
                argv,
                cwd=plan.output_dir,
                timeout=float(plan.timeout_seconds),
                proxy_url=self._settings.egress_proxy_url,
                cancel_token=cancel_token,
            )
        except SubprocessTimeoutError as exc:
            raise AdapterError(
                ErrorCode.ERR_TIMEOUT, str(exc), engine_id=self.id, retryable=True
            ) from exc
        except SubprocessCancelledError as exc:
            raise AdapterError(ErrorCode.ERR_INTERNAL, str(exc), engine_id=self.id) from exc

        if res.returncode != 0:
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                (res.stderr or res.stdout)[-400:],
                engine_id=self.id,
                retryable=True,
            )

        downloaded_files = [p for p in plan.output_dir.rglob("*") if p.is_file()]
        if not downloaded_files:
            raise AdapterError(
                ErrorCode.ERR_INTERNAL,
                "gallery-dl completed with zero files.",
                engine_id=self.id,
            )

        total_bytes = sum(p.stat().st_size for p in downloaded_files)
        if total_bytes > plan.max_bytes:
            raise AdapterError(
                ErrorCode.ERR_QUOTA_EXCEEDED,
                f"Gallery total ({total_bytes} bytes) exceeded quota ({plan.max_bytes} bytes).",
                engine_id=self.id,
            )

        for p in downloaded_files:
            magic_mime, is_exec = detect_magic_mime(p)
            if is_exec:
                p.unlink(missing_ok=True)
                raise AdapterError(
                    ErrorCode.ERR_VALIDATION_FAILED,
                    f"Blocked dangerous executable inside gallery ({magic_mime}).",
                    engine_id=self.id,
                )

        if len(downloaded_files) == 1:
            single = downloaded_files[0]
            magic_mime, _ = detect_magic_mime(single)
            return DownloadArtifact(
                local_path=single,
                filename=sanitize_filename(single.name),
                mime_type=magic_mime or mimetypes.guess_type(single.name)[0] or "image/jpeg",
                size_bytes=single.stat().st_size,
            )

        # Package multi-file gallery with ZIP_STORED (images are already compressed)
        zip_path = plan.output_dir / f"{sanitize_filename(ctx.domain)}_gallery.zip"
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for idx, item in enumerate(sorted(downloaded_files), start=1):
                arcname = f"{idx:04d}_{sanitize_filename(item.name)}"
                zf.write(item, arcname=arcname)

        return DownloadArtifact(
            local_path=zip_path,
            filename=zip_path.name,
            mime_type="application/zip",
            size_bytes=zip_path.stat().st_size,
            extra_files=downloaded_files,
        )

    async def health_check(self) -> HealthReport:
        exe = find_binary(self._settings.gallery_dl_bin)
        if not exe:
            return HealthReport(
                engine_id=self.id,
                installation_status=InstallationStatus.MISSING,
                version="missing",
                healthy=False,
                detail="gallery-dl not found on PATH.",
            )
        res = await run_safe_subprocess([exe, "--version"], timeout=5.0)
        version = res.stdout.strip() or "unknown"
        self._version = version
        return HealthReport(
            engine_id=self.id,
            installation_status=InstallationStatus.INSTALLED,
            version=version,
            healthy=(res.returncode == 0),
            detail=f"gallery-dl {version}",
        )
