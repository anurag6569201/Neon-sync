"""Safe argv-only subprocess execution with process-group cancellation & env scrubbing."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

_SAFE_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "DENO_DIR",
        "XDG_CACHE_HOME",
    }
)


class SubprocessTimeoutError(TimeoutError):
    """Raised when a child process exceeds its allotted wall-clock budget."""


class SubprocessCancelledError(asyncio.CancelledError):
    """Raised when a child process is terminated via a CancellationToken."""


class CancellationToken:
    """Cooperative + preemptive job cancellation handle."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None

    def cancel(self, reason: str = "cancelled by user") -> None:
        self.reason = reason
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


@dataclass
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str


def build_scrubbed_env(
    *,
    proxy_url: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Construct a minimal environment stripping app secrets (DB, AWS, API keys)."""
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_ALLOWLIST}
    if proxy_url:
        env["HTTP_PROXY"] = proxy_url
        env["HTTPS_PROXY"] = proxy_url
        env["http_proxy"] = proxy_url
        env["https_proxy"] = proxy_url
        env["ALL_PROXY"] = proxy_url
        env["NO_PROXY"] = ""
    if extra_env:
        env.update(extra_env)
    return env


def _kill_process_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), sig)
        else:
            proc.terminate() if sig == signal.SIGTERM else proc.kill()
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _terminate_gracefully(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
    if proc.returncode is not None:
        return
    _kill_process_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except TimeoutError:
        _kill_process_group(proc, signal.SIGKILL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            pass


LineCallback = Callable[[str], Awaitable[None] | None]


async def _drain_stream(
    stream: asyncio.StreamReader | None,
    collector: list[str],
    callback: LineCallback | None = None,
    max_bytes: int = 16 * 1024 * 1024,
) -> None:
    if stream is None:
        return
    total = 0
    while True:
        line_bytes = await stream.readline()
        if not line_bytes:
            break
        text = line_bytes.decode("utf-8", errors="replace")
        if total < max_bytes:
            collector.append(text)
            total += len(line_bytes)
        if callback is not None:
            res = callback(text.rstrip("\r\n"))
            if asyncio.iscoroutine(res):
                await res


async def run_safe_subprocess(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 30.0,
    proxy_url: str | None = None,
    extra_env: dict[str, str] | None = None,
    cancel_token: CancellationToken | None = None,
    on_stdout_line: LineCallback | None = None,
    on_stderr_line: LineCallback | None = None,
) -> SubprocessResult:
    """Execute an external binary with argv array (never shell=True) and group cleanup."""
    if not argv:
        raise ValueError("argv must not be empty")
    if cancel_token is not None and cancel_token.is_cancelled:
        raise SubprocessCancelledError(cancel_token.reason or "cancelled")

    env = build_scrubbed_env(proxy_url=proxy_url, extra_env=extra_env)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    stdout_task = asyncio.create_task(_drain_stream(proc.stdout, stdout_chunks, on_stdout_line))
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr, stderr_chunks, on_stderr_line))
    wait_task = asyncio.create_task(proc.wait())
    cancel_task = asyncio.create_task(cancel_token.wait()) if cancel_token else None

    try:
        pending_waits = {wait_task}
        if cancel_task is not None:
            pending_waits.add(cancel_task)

        done, _ = await asyncio.wait(
            pending_waits,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            await _terminate_gracefully(proc)
            raise SubprocessTimeoutError(f"Command '{argv[0]}' timed out after {timeout}s")

        if cancel_task is not None and cancel_task in done:
            await _terminate_gracefully(proc)
            raise SubprocessCancelledError(cancel_token.reason or "cancelled")

        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return SubprocessResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
        )
    except BaseException:
        await _terminate_gracefully(proc)
        for task in (stdout_task, stderr_task, wait_task):
            if not task.done():
                task.cancel()
        raise
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
