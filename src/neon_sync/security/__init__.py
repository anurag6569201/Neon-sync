"""Security foundation: SSRF protection, safe egress proxy, subprocess & file guards."""

from .egress_proxy import ProxyDeniedError, SafeEgressProxy
from .file_guard import (
    DiskQuotaError,
    FileValidationError,
    build_content_disposition,
    check_free_disk_floor,
    detect_magic_mime,
    ensure_safe_path,
    sanitize_filename,
)
from .subprocess_guard import (
    CancellationToken,
    SubprocessCancelledError,
    SubprocessResult,
    SubprocessTimeoutError,
    run_safe_subprocess,
)
from .url_validator import (
    UrlValidationError,
    ValidatedUrl,
    is_public_ip,
    validate_and_canonicalize_url,
)

__all__ = [
    "CancellationToken",
    "DiskQuotaError",
    "FileValidationError",
    "ProxyDeniedError",
    "SafeEgressProxy",
    "SubprocessCancelledError",
    "SubprocessResult",
    "SubprocessTimeoutError",
    "UrlValidationError",
    "ValidatedUrl",
    "build_content_disposition",
    "check_free_disk_floor",
    "detect_magic_mime",
    "ensure_safe_path",
    "is_public_ip",
    "run_safe_subprocess",
    "sanitize_filename",
    "validate_and_canonicalize_url",
]
