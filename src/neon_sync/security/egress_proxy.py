"""Hardened Safe Egress HTTP & CONNECT Proxy.

Resolves DNS inside the proxy, verifies that EVERY resolved IP is a globally routable
public unicast address (blocking RFC1918, loopback, link-local/IMDS, CGNAT, and
IPv4-mapped IPv6), and opens the upstream TCP connection directly to the verified IP.
This eliminates TOCTOU DNS rebinding for all downstream CLI tools (yt-dlp, curl_cffi,
gallery-dl, aria2c, Playwright).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from urllib.parse import urlsplit

from .url_validator import ALLOWED_PORTS, BLOCKED_HOSTNAME_SUFFIXES, BLOCKED_HOSTNAMES, is_public_ip

log = logging.getLogger("neon_sync.egress_proxy")

MAX_HEADER_BYTES = 64 * 1024
HOP_BY_HOP_HEADERS = frozenset(
    {
        "proxy-authorization",
        "proxy-connection",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
    }
)


class ProxyDeniedError(Exception):
    """Raised when an outbound connection violates egress security policy."""


def _split_authority(authority: str, default_port: int) -> tuple[str, int]:
    authority = authority.strip()
    if not authority:
        raise ProxyDeniedError("empty authority")
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise ProxyDeniedError("invalid IPv6 authority")
        host = authority[1:end]
        rest = authority[end + 1 :]
        if rest.startswith(":"):
            try:
                port = int(rest[1:])
            except ValueError as exc:
                raise ProxyDeniedError("invalid port") from exc
        elif rest == "":
            port = default_port
        else:
            raise ProxyDeniedError("malformed IPv6 authority")
        return host, port
    if authority.count(":") == 1:
        host, raw_port = authority.rsplit(":", 1)
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ProxyDeniedError("invalid port") from exc
        return host, port
    if ":" in authority:
        raise ProxyDeniedError("ambiguous authority")
    return authority, default_port


class SafeEgressProxy:
    """Async HTTP/HTTPS-CONNECT proxy enforcing public-IP-only egress."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3128,
        *,
        allowed_ports: frozenset[int] = ALLOWED_PORTS,
        allow_private: bool = False,
        max_bytes_per_conn: int | None = None,
        dns_timeout: float = 8.0,
        connect_timeout: float = 12.0,
    ) -> None:
        self.host = host
        self.port = port
        self.allowed_ports = allowed_ports
        self.allow_private = allow_private
        self.max_bytes_per_conn = max_bytes_per_conn
        self.dns_timeout = dns_timeout
        self.connect_timeout = connect_timeout
        self._server: asyncio.AbstractServer | None = None
        self.blocked_count: int = 0
        self.allowed_count: int = 0

    @property
    def bound_port(self) -> int:
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self.port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.bound_port}"

    async def resolve_and_validate(self, host: str, port: int) -> tuple[int, str]:
        if not self.allow_private and port not in self.allowed_ports:
            raise ProxyDeniedError(f"port {port} denied")

        clean_host = host.strip().lower().rstrip(".")
        if not clean_host:
            raise ProxyDeniedError("empty host")

        if not self.allow_private and (
            clean_host in BLOCKED_HOSTNAMES
            or any(clean_host.endswith(suffix) for suffix in BLOCKED_HOSTNAME_SUFFIXES)
        ):
            raise ProxyDeniedError(f"blocked hostname: {clean_host}")

        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(clean_host, port, type=socket.SOCK_STREAM),
                timeout=self.dns_timeout,
            )
        except (TimeoutError, socket.gaierror, OSError) as exc:
            raise ProxyDeniedError(f"DNS resolution failed for {clean_host}") from exc

        candidates: list[tuple[int, str]] = []
        for family, _socktype, _proto, _canon, sockaddr in infos:
            ip_str = sockaddr[0]
            if not self.allow_private and not is_public_ip(ip_str):
                # Fail closed if ANY resolved address is private (blocks dual-record rebinding)
                raise ProxyDeniedError(f"non-public IP denied: {ip_str}")
            pair = (family, ip_str)
            if pair not in candidates:
                candidates.append(pair)

        if not candidates:
            raise ProxyDeniedError(f"no valid addresses for {clean_host}")
        return candidates[0]

    async def _relay(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        transferred = 0
        try:
            while True:
                chunk = await reader.read(64 * 1024)
                if not chunk:
                    break
                transferred += len(chunk)
                if self.max_bytes_per_conn is not None and transferred > self.max_bytes_per_conn:
                    log.warning("Closing proxy tunnel: exceeded max_bytes_per_conn")
                    break
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, OSError, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.write_eof()
            except (AttributeError, OSError, RuntimeError):
                pass

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            try:
                raw_headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15.0)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError) as exc:
                raise ProxyDeniedError("invalid or oversized request headers") from exc

            if len(raw_headers) > MAX_HEADER_BYTES:
                raise ProxyDeniedError("headers too large")

            lines = raw_headers.decode("iso-8859-1").split("\r\n")
            parts = lines[0].split(" ", 2)
            if len(parts) != 3:
                raise ProxyDeniedError("malformed request line")
            method, target, version = parts

            headers: list[tuple[str, str]] = []
            for line in lines[1:]:
                if not line:
                    continue
                if ":" not in line:
                    raise ProxyDeniedError("malformed header")
                name, value = line.split(":", 1)
                headers.append((name.strip(), value.strip()))

            if method.upper() == "CONNECT":
                host, port = _split_authority(target, 443)
                family, verified_ip = await self.resolve_and_validate(host, port)
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(verified_ip, port, family=family),
                    timeout=self.connect_timeout,
                )
                self.allowed_count += 1
                writer.write(f"{version} 200 Connection Established\r\n\r\n".encode("ascii"))
                await writer.drain()
            else:
                parsed = urlsplit(target)
                if parsed.scheme.lower() != "http" or not parsed.hostname:
                    raise ProxyDeniedError("absolute http URL required")
                port = parsed.port or 80
                family, verified_ip = await self.resolve_and_validate(parsed.hostname, port)
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(verified_ip, port, family=family),
                    timeout=self.connect_timeout,
                )
                self.allowed_count += 1
                path = parsed.path or "/"
                if parsed.query:
                    path = f"{path}?{parsed.query}"
                filtered = [
                    (k, v) for k, v in headers if k.lower() not in HOP_BY_HOP_HEADERS
                ]
                head_lines = [f"{method} {path} {version}"]
                head_lines.extend(f"{k}: {v}" for k, v in filtered)
                head_lines.append("Connection: close")
                upstream_writer.write(("\r\n".join(head_lines) + "\r\n\r\n").encode("iso-8859-1"))
                await upstream_writer.drain()

            t1 = asyncio.create_task(self._relay(reader, upstream_writer))
            t2 = asyncio.create_task(self._relay(upstream_reader, writer))
            await asyncio.gather(t1, t2)
        except ProxyDeniedError as exc:
            self.blocked_count += 1
            log.warning("Egress proxy denied request: %s", exc)
            try:
                body = f"Blocked by Safe Egress Proxy: {exc}".encode("utf-8", errors="replace")
                resp = (
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"Connection: close\r\n\r\n"
                    + body
                )
                writer.write(resp)
                await writer.drain()
            except (ConnectionError, OSError):
                pass
        except (ConnectionError, OSError, TimeoutError, ValueError):
            try:
                writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
            except (ConnectionError, OSError):
                pass
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def start(self) -> SafeEgressProxy:
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
            limit=MAX_HEADER_BYTES,
        )
        log.info("Safe Egress Proxy listening on %s", self.url)
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


def cli_main() -> None:
    parser = argparse.ArgumentParser(description="Neon Sync Safe Egress Proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3128)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async def _serve() -> None:
        proxy = SafeEgressProxy(host=args.host, port=args.port)
        await proxy.start()
        assert proxy._server is not None
        async with proxy._server:
            await proxy._server.serve_forever()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
