"""Strict URL validation, SSRF ingress checks, and platform URL canonicalization."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from neon_sync.models import ErrorCode

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
        "metadata.goog",
        "metadata.azure.com",
        "instance-data",
    }
)

BLOCKED_HOSTNAME_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
)

# Extra non-public networks not always caught uniformly across Python/OS versions
_EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT / Tailscale
    ipaddress.ip_network("192.0.0.0/24"),   # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),  # Benchmarking
    ipaddress.ip_network("64:ff9b::/96"),   # NAT64
    ipaddress.ip_network("64:ff9b:1::/48"), # Local-use NAT64
)

_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "dclid",
        "mc_cid",
        "mc_eid",
        "si",
        "igsh",
        "igshid",
        "ref_src",
        "ref_url",
    }
)

_ALT_TWITTER_HOSTS = frozenset(
    {
        "vxtwitter.com",
        "www.vxtwitter.com",
        "fxtwitter.com",
        "www.fxtwitter.com",
        "fixvx.com",
        "www.fixvx.com",
        "fixupx.com",
        "www.fixupx.com",
    }
)


class UrlValidationError(ValueError):
    """Raised when a user-supplied URL fails syntax or SSRF safety validation."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedUrl:
    original_url: str
    canonical_url: str
    scheme: str
    hostname: str
    domain: str
    port: int
    path: str


def is_public_ip(raw_ip: str) -> bool:
    """Return True strictly if raw_ip is a globally routable public unicast IP."""
    try:
        ip = ipaddress.ip_address(raw_ip.strip().strip("[]"))
    except ValueError:
        return False

    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1 or ::ffff:169.254.169.254)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False

    for net in _EXTRA_BLOCKED_NETWORKS:
        if ip.version == net.version and ip in net:
            return False

    return True


def _extract_registrable_domain(hostname: str) -> str:
    host = hostname.lower().rstrip(".")
    host = host.removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 2:
        # Handle common two-label public suffixes (co.uk, com.au, co.jp, etc.)
        if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "edu", "ac"} and len(parts[-1]) == 2:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])
    return host


def _canonicalize(scheme: str, host: str, port: int, path: str, query: str) -> str:
    # Strip known tracking query parameters
    cleaned_params = [
        (k, v)
        for k, v in parse_qsl(query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]

    # Normalize common shortlinks & mirror domains
    segments = [s for s in path.split("/") if s]
    if host in {"youtu.be", "www.youtu.be"} and segments:
        video_id = segments[0]
        host = "www.youtube.com"
        path = "/watch"
        cleaned_params = [("v", video_id)] + [(k, v) for k, v in cleaned_params if k != "v"]
        scheme = "https"
        port = 443
    elif host in _ALT_TWITTER_HOSTS:
        host = "x.com"
        scheme = "https"
        port = 443

    default_port = 443 if scheme == "https" else 80
    # Format IPv6 literal properly in netloc
    host_netloc = f"[{host}]" if ":" in host else host
    netloc = host_netloc if port == default_port else f"{host_netloc}:{port}"
    canonical_query = urlencode(cleaned_params, doseq=True)
    return urlunsplit((scheme, netloc, path or "/", canonical_query, ""))


def validate_and_canonicalize_url(
    raw_url: str,
    *,
    allow_private: bool = False,
    max_length: int = 4096,
) -> ValidatedUrl:
    """Validate scheme, syntax, host safety, and return canonicalized URL."""
    if not isinstance(raw_url, str):
        raise UrlValidationError(ErrorCode.ERR_INVALID_URL, "URL must be a string.")

    cleaned = raw_url.strip()
    if not cleaned:
        raise UrlValidationError(ErrorCode.ERR_INVALID_URL, "URL cannot be empty.")
    if len(cleaned) > max_length:
        raise UrlValidationError(
            ErrorCode.ERR_INVALID_URL,
            f"URL exceeds maximum allowed length of {max_length} characters.",
        )
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise UrlValidationError(
            ErrorCode.ERR_INVALID_URL,
            "URL contains whitespace or control characters.",
        )

    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlValidationError(
            ErrorCode.ERR_INVALID_URL,
            f"Unsupported URL scheme '{parsed.scheme}'. Only http and https are allowed.",
        )

    if parsed.username is not None or parsed.password is not None:
        raise UrlValidationError(
            ErrorCode.ERR_INVALID_URL,
            "Embedded credentials in URLs are not permitted.",
        )

    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise UrlValidationError(ErrorCode.ERR_INVALID_URL, "URL must include a valid hostname.")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UrlValidationError(ErrorCode.ERR_INVALID_URL, "URL contains an invalid port.") from exc

    if not allow_private and port not in ALLOWED_PORTS:
        raise UrlValidationError(
            ErrorCode.ERR_SSRF_BLOCKED,
            f"Port {port} is not allowed. Only ports 80 and 443 are permitted.",
        )

    if not allow_private:
        if hostname in BLOCKED_HOSTNAMES or any(
            hostname.endswith(suffix) for suffix in BLOCKED_HOSTNAME_SUFFIXES
        ):
            raise UrlValidationError(
                ErrorCode.ERR_SSRF_BLOCKED,
                f"Hostname '{hostname}' is blocked by SSRF policy.",
            )

        # Reject integer/hex/octal obfuscated IP literals as well as standard IPv4/IPv6 literals
        try:
            ipaddress.ip_address(hostname)
            is_literal = True
        except ValueError:
            is_literal = False

        if is_literal and not is_public_ip(hostname):
            raise UrlValidationError(
                ErrorCode.ERR_SSRF_BLOCKED,
                f"IP address '{hostname}' is not a public routable address.",
            )

        # Catch decimal/octal IPv4 obfuscation like http://2130706433 or http://0x7f000001
        if "." not in hostname and ":" not in hostname:
            try:
                int(hostname, 0)
            except ValueError:
                pass
            else:
                raise UrlValidationError(
                    ErrorCode.ERR_SSRF_BLOCKED,
                    "Numeric IP literal hostnames are blocked by SSRF policy.",
                )

    canonical = _canonicalize(scheme, hostname, port, parsed.path, parsed.query)
    canon_parsed = urlsplit(canonical)
    canon_host = (canon_parsed.hostname or hostname).lower()
    canon_port = canon_parsed.port or (443 if canon_parsed.scheme == "https" else 80)

    return ValidatedUrl(
        original_url=cleaned,
        canonical_url=canonical,
        scheme=canon_parsed.scheme,
        hostname=canon_host,
        domain=_extract_registrable_domain(canon_host),
        port=canon_port,
        path=canon_parsed.path or "/",
    )
