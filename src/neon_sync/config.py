"""Application configuration and production safety quotas."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings for the Universal Media Extraction Engine."""

    model_config = SettingsConfigDict(
        env_prefix="NEON_SYNC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Service identity & API
    app_name: str = "Neon Sync Universal Media Extraction Engine"
    environment: str = "development"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # Relative links work on localhost, custom domains, and HTTPS reverse proxies.
    artifact_base_url: str = ""

    # Security & Safe Egress Proxy
    egress_proxy_url: str | None = None
    egress_proxy_host: str = "127.0.0.1"
    egress_proxy_port: int = 3128
    allow_private_addresses: bool = False  # Strictly False in production!
    max_url_length: int = 4096
    max_redirects: int = 6

    # Storage & scratch directories
    scratch_dir: Path = Field(default=Path("./scratch/jobs"))
    storage_dir: Path = Field(default=Path("./data/artifacts"))
    artifact_ttl_seconds: int = 6 * 60 * 60  # 6 hours default
    min_free_disk_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB dev floor (10 GiB+ in prod)

    # Resource quotas & timeouts
    max_job_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB
    max_playlist_items: int = 50
    # Metadata browsing is paged independently of the per-download item limit.
    playlist_probe_page_size: int = Field(default=50, ge=1, le=200)
    max_gallery_items: int = 100
    max_livestream_duration_seconds: int = 1800  # 30 min live recording cap
    probe_timeout_seconds: int = 40
    download_timeout_seconds: int = 1800
    ffmpeg_timeout_seconds: int = 600
    direct_aria2_threshold_bytes: int = 10 * 1024 * 1024  # >= 10 MiB uses aria2c

    # Browser fallback (Tier-3 Playwright)
    enable_browser_fallback: bool = True
    browser_timeout_seconds: int = 18
    browser_max_contexts: int = 2
    browser_recycle_after_jobs: int = 50

    # Persistent job store and S3 / Cloudflare R2 storage
    job_db_path: Path | None = None
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str = "auto"
    s3_key_prefix: str = "neon-sync/artifacts/"

    # Cloudflare Cloud Stack (R2 Storage & Turnstile Bot Protection)
    cf_r2_bucket: str | None = None
    cf_r2_access_key_id: str | None = None
    cf_r2_secret_access_key: str | None = None
    cf_turnstile_site_key: str | None = None
    cf_turnstile_secret: str | None = None
    cf_turnstile_hostnames: str = "localhost,127.0.0.1"

    # Concurrency & rate limiting
    max_concurrent_probes: int = 8
    max_concurrent_downloads: int = 3
    max_pending_jobs: int = Field(default=100, ge=1)
    rate_limit_probes_per_minute: int = 30
    rate_limit_downloads_per_hour: int = 20
    probe_cache_ttl_seconds: int = 300  # 5 minutes

    # Engine binary overrides (auto-discovered via PATH when None)
    ytdlp_bin: str = "yt-dlp"
    gallery_dl_bin: str = "gallery-dl"
    streamlink_bin: str = "streamlink"
    nm3u8dl_bin: str = "N_m3u8DL-RE"
    aria2c_bin: str = "aria2c"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    deno_bin: str = "deno"


def get_settings() -> Settings:
    return Settings()
