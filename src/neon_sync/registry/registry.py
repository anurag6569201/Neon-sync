"""Universal Engine Registry with capability filtering, health telemetry & circuit breakers."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

from neon_sync.adapters.base import ExtractorAdapter, PostProcessorAdapter, UrlContext
from neon_sync.models import (
    EngineCapability,
    EngineManifest,
    HealthReport,
    HealthState,
    InstallationStatus,
    MatchScore,
)


@dataclass
class CandidateEngine:
    adapter: ExtractorAdapter
    match: MatchScore
    composite_score: float


class _DomainCircuitState:
    """Sliding-window failure & circuit breaker state per (engine_id, domain)."""

    def __init__(self, window_seconds: float = 300.0, cooldown_seconds: float = 120.0) -> None:
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.events: deque[tuple[float, bool]] = deque()  # (monotonic_ts, success)
        self.consecutive_failures: int = 0
        self.opened_at: float | None = None

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def record(self, success: bool) -> None:
        now = time.monotonic()
        self._prune(now)
        self.events.append((now, success))
        if success:
            self.consecutive_failures = 0
            self.opened_at = None
        else:
            self.consecutive_failures += 1
            if self.consecutive_failures >= 5 or (
                len(self.events) >= 5 and self.error_rate() >= 0.6
            ):
                self.opened_at = now

    def error_rate(self) -> float:
        now = time.monotonic()
        self._prune(now)
        if not self.events:
            return 0.0
        failures = sum(1 for _, ok in self.events if not ok)
        return failures / len(self.events)

    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if (time.monotonic() - self.opened_at) >= self.cooldown_seconds:
            # Half-open recovery
            self.opened_at = None
            return False
        return True


class EngineRegistry:
    """Central capability registry managing all extraction & post-processing adapters."""

    def __init__(self) -> None:
        self._extractors: dict[str, ExtractorAdapter] = {}
        self._postprocessors: dict[str, PostProcessorAdapter] = {}
        self._health_reports: dict[str, HealthReport] = {}
        self._circuits: dict[tuple[str, str], _DomainCircuitState] = {}
        self._engine_circuits: dict[str, _DomainCircuitState] = {}
        self._outdated_flags: dict[str, bool] = {}

    def register_extractor(self, adapter: ExtractorAdapter) -> None:
        self._extractors[adapter.id] = adapter

    def register_postprocessor(self, adapter: PostProcessorAdapter) -> None:
        self._postprocessors[adapter.id] = adapter

    def get_extractor(self, engine_id: str) -> ExtractorAdapter | None:
        return self._extractors.get(engine_id)

    def get_postprocessor(self, engine_id: str) -> PostProcessorAdapter | None:
        return self._postprocessors.get(engine_id)

    def list_extractors(self) -> list[ExtractorAdapter]:
        return list(self._extractors.values())

    def _enriched_manifest(self, adapter: ExtractorAdapter | PostProcessorAdapter) -> EngineManifest:
        manifest = adapter.manifest().model_copy(deep=True)
        report = self._health_reports.get(adapter.id)
        if report is not None:
            manifest.installation_status = report.installation_status
            manifest.version = report.version
            manifest.health_status.last_checked_at = report.checked_at
            if not report.healthy:
                manifest.health_status.state = HealthState.UNAVAILABLE

        circuit = self._engine_circuits.get(adapter.id)
        if circuit is not None:
            manifest.health_status.consecutive_failures = circuit.consecutive_failures
            manifest.health_status.error_rate_5m = round(circuit.error_rate(), 4)
            if manifest.health_status.state != HealthState.UNAVAILABLE:
                if circuit.is_open():
                    manifest.health_status.state = HealthState.CIRCUIT_OPEN
                elif circuit.error_rate() >= 0.25:
                    manifest.health_status.state = HealthState.DEGRADED

        manifest.health_status.outdated_detected = self._outdated_flags.get(adapter.id, False)
        return manifest

    def list_manifests(self) -> list[EngineManifest]:
        all_adapters: list[ExtractorAdapter | PostProcessorAdapter] = [
            *self._extractors.values(),
            *self._postprocessors.values(),
        ]
        return [self._enriched_manifest(a) for a in all_adapters]

    async def refresh_health(self) -> list[HealthReport]:
        all_adapters: list[ExtractorAdapter | PostProcessorAdapter] = [
            *self._extractors.values(),
            *self._postprocessors.values(),
        ]
        reports = await asyncio.gather(*(a.health_check() for a in all_adapters))
        for report in reports:
            self._health_reports[report.engine_id] = report
        return list(reports)

    def record_outcome(
        self,
        engine_id: str,
        domain: str,
        *,
        success: bool,
        outdated: bool = False,
    ) -> None:
        key = (engine_id, domain)
        self._circuits.setdefault(key, _DomainCircuitState()).record(success)
        self._engine_circuits.setdefault(engine_id, _DomainCircuitState()).record(success)
        if outdated:
            self._outdated_flags[engine_id] = True
        elif success and self._outdated_flags.get(engine_id):
            self._outdated_flags[engine_id] = False

    def is_circuit_open(self, engine_id: str, domain: str) -> bool:
        dom_state = self._circuits.get((engine_id, domain))
        if dom_state and dom_state.is_open():
            return True
        eng_state = self._engine_circuits.get(engine_id)
        return bool(eng_state and eng_state.is_open())

    def select_candidates(
        self,
        ctx: UrlContext,
        *,
        required_capabilities: set[EngineCapability] | None = None,
        preferred_engine: str | None = None,
        allow_browser_fallback: bool = False,
    ) -> list[CandidateEngine]:
        """Filter and rank candidate extractors by capability, match confidence, health & cost."""
        required = required_capabilities or {EngineCapability.EXTRACT_METADATA}
        candidates: list[CandidateEngine] = []

        for adapter in self._extractors.values():
            manifest = self._enriched_manifest(adapter)

            if manifest.installation_status != InstallationStatus.INSTALLED:
                continue
            if manifest.health_status.state == HealthState.UNAVAILABLE:
                continue
            if not allow_browser_fallback and manifest.cost_profile.requires_browser:
                continue
            if not required.issubset(set(manifest.capabilities)):
                continue
            if self.is_circuit_open(adapter.id, ctx.domain) and adapter.id != preferred_engine:
                continue

            match = adapter.quick_match(ctx)
            if match.confidence <= 0 and adapter.id != preferred_engine:
                continue

            # Composite score:
            # 50% match confidence + 25% base priority + 15% reliability - 10% latency cost
            match_norm = match.confidence / 100.0
            priority_norm = manifest.priority / 1000.0
            dom_circuit = self._circuits.get((adapter.id, ctx.domain))
            err_rate = dom_circuit.error_rate() if dom_circuit else manifest.health_status.error_rate_5m
            reliability = max(0.0, 1.0 - err_rate)
            cost_penalty = min(1.0, manifest.cost_profile.avg_probe_ms / 10000.0)

            score = (
                match_norm * 0.50
                + priority_norm * 0.25
                + reliability * 0.15
                - cost_penalty * 0.10
            )
            if preferred_engine and adapter.id == preferred_engine:
                score += 1.0  # Explicitly requested engine takes precedence

            candidates.append(
                CandidateEngine(
                    adapter=adapter,
                    match=match,
                    composite_score=round(score, 4),
                )
            )

        candidates.sort(key=lambda c: c.composite_score, reverse=True)
        return candidates

    def mark_health_for_test(
        self,
        engine_id: str,
        *,
        installed: bool = True,
        healthy: bool = True,
        version: str = "test",
    ) -> None:
        self._health_reports[engine_id] = HealthReport(
            engine_id=engine_id,
            installation_status=(
                InstallationStatus.INSTALLED if installed else InstallationStatus.MISSING
            ),
            version=version,
            healthy=healthy,
            checked_at=datetime.now(UTC),
        )
