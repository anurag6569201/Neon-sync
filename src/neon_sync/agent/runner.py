"""Neon Sync AI Agent v2 — Multi-Step Autonomous Media Workflow & Transcript SSE Runner."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from neon_sync.adapters import AdapterError
from neon_sync.config import Settings
from neon_sync.models import DownloadRequest, JobRecord, JobStatus, NormalizedMediaOutput, ProbeRequest
from neon_sync.pipeline import ExtractionOrchestrator, JobManager, normalize_subtitle_to_transcript
from neon_sync.pipeline.tiers import TierLimitError, TierManager
from neon_sync.security import UrlValidationError, validate_and_canonicalize_url

from .planner import AgentRequest, plan_agent_action, summarize_text_with_llm


class AgentEvent(BaseModel):
    type: Literal["thought", "plan", "tool", "progress", "stream", "answer", "job", "error", "done"]
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class NeonSyncAgent:
    """Executes multi-step natural-language media workflows over Neon Sync's internal pipeline."""

    def __init__(
        self,
        settings: Settings,
        orchestrator: ExtractionOrchestrator,
        job_manager: JobManager,
        tier_manager: TierManager,
    ) -> None:
        self._settings = settings
        self._orchestrator = orchestrator
        self._job_manager = job_manager
        self._tier_manager = tier_manager

    async def _fetch_live_transcript(self, probe_out: NormalizedMediaOutput) -> str | None:
        """SSRF-safely fetch and normalize the best available subtitle track for grounded AI synthesis."""
        if not probe_out.subtitles:
            return None
        # Prefer manual English captions, then any manual captions, then auto English, then first available
        ordered = sorted(
            probe_out.subtitles,
            key=lambda s: (
                0 if not s.is_auto_generated else 1,
                0 if s.language.lower().startswith("en") else 1,
            ),
        )
        chosen = next((s for s in ordered if s.url), None)
        if chosen is None or not chosen.url:
            return None
        try:
            validated = validate_and_canonicalize_url(
                chosen.url,
                allow_private=self._settings.allow_private_addresses,
                max_length=self._settings.max_url_length,
            )
            client_kwargs: dict[str, Any] = {"follow_redirects": True, "max_redirects": 4, "timeout": 10.0}
            if self._settings.egress_proxy_url:
                client_kwargs["proxy"] = self._settings.egress_proxy_url
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(validated.canonical_url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200 or not resp.text:
                    return None
                clean = normalize_subtitle_to_transcript(resp.text)
                return clean if clean and len(clean) >= 20 else None
        except Exception:  # noqa: BLE001
            return None

    async def _build_corpus(self, probe_out: NormalizedMediaOutput, transcript_text: str | None) -> str:
        chapters_str = ""
        if probe_out.metadata.chapters:
            ch_lines = [
                f"- {int(ch.start_seconds)}s: {ch.title}"
                for ch in probe_out.metadata.chapters[:25]
            ]
            chapters_str = "Chapters:\n" + "\n".join(ch_lines)

        parts = [
            f"Title: {probe_out.title or 'Unknown'}",
            f"Source: {probe_out.source.domain}",
            f"Author: {probe_out.author.name if probe_out.author else 'Unknown'}",
            f"Duration: {int(probe_out.duration_seconds)}s" if probe_out.duration_seconds else "",
            chapters_str,
            f"Description:\n{probe_out.description[:3000]}" if probe_out.description else "",
            f"Spoken Transcript:\n{transcript_text[:20000]}" if transcript_text else "",
        ]
        return "\n\n".join(p for p in parts if p)

    async def _execute_single_job(
        self,
        *,
        url: str,
        raw_params: dict[str, Any],
        client_key: str,
        pro_key: str | None,
        byok_gemini_key: str | None,
        account_tier: str | None,
    ) -> tuple[JobRecord | None, str | None, dict[str, Any] | None]:
        dl_kwargs = {k: v for k, v in raw_params.items() if v is not None}
        try:
            dl_req = DownloadRequest(url=url, **dl_kwargs)
            self._tier_manager.check_and_consume_download(
                client_key,
                dl_req,
                pro_key=pro_key,
                byok_key=byok_gemini_key,
                account_tier=account_tier,
            )
        except TierLimitError as exc:
            return None, exc.message, {"code": exc.code, "upgrade_hint": exc.upgrade_hint}
        except Exception as exc:  # noqa: BLE001
            return None, f"Invalid extraction plan: {exc}", None

        record = await self._job_manager.submit(dl_req)
        return record, None, None

    async def run_stream(
        self,
        request: AgentRequest,
        *,
        client_key: str,
        pro_key: str | None = None,
        byok_gemini_key: str | None = None,
        account_tier: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(type="thought", message="Understanding your request and extracting target URL…")

        try:
            plan = await plan_agent_action(
                request,
                settings=self._settings,
                byok_gemini_key=byok_gemini_key,
            )
            validate_and_canonicalize_url(
                plan.url,
                allow_private=self._settings.allow_private_addresses,
                max_length=self._settings.max_url_length,
            )
        except (ValueError, UrlValidationError) as exc:
            yield AgentEvent(type="error", message=str(exc))
            yield AgentEvent(type="done", message="Stopped")
            return

        yield AgentEvent(
            type="plan",
            message=plan.explanation,
            data={
                "action": plan.action,
                "url": plan.url,
                "provider": plan.provider_used,
                "output_style": plan.output_style,
                "step_count": len(plan.steps) or 1,
                "download_params": plan.download_params,
            },
        )

        # Tool 1: Always inspect metadata cleanly first
        yield AgentEvent(type="tool", message="Inspecting source metadata via Neon Sync Core…", data={"tool": "inspect_media"})
        try:
            probe_out = await self._orchestrator.probe(ProbeRequest(url=plan.url, include_subtitles=True))
        except (AdapterError, UrlValidationError) as exc:
            yield AgentEvent(type="error", message=str(exc))
            yield AgentEvent(type="done", message="Failed")
            return

        yield AgentEvent(
            type="thought",
            message=(
                f"Detected {probe_out.content_type.value}: “{probe_out.title or probe_out.url}” "
                f"({len(probe_out.available_formats)} streams, {len(probe_out.subtitles)} captions) via Neon Sync."
            ),
            data={
                "title": probe_out.title,
                "content_type": probe_out.content_type.value,
                "engine": "neon-sync",
                "formats": len(probe_out.available_formats),
                "subtitles": len(probe_out.subtitles),
            },
        )

        if plan.action == "probe":
            yield AgentEvent(
                type="answer",
                message=(
                    f"Found **{probe_out.title or probe_out.url}** ({probe_out.content_type.value}) "
                    f"with {len(probe_out.available_formats)} format(s) and {len(probe_out.subtitles)} subtitle track(s)."
                ),
                data={"probe": probe_out.model_dump(mode="json")},
            )
            yield AgentEvent(type="done", message="Complete")
            return

        # Tool 2: Live Network Stream Action (VLC Mode!)
        if plan.action in {"stream", "stream_and_summarize"}:
            mode = plan.stream_mode or ("audio" if probe_out.content_type.value == "audio" else "video")
            yield AgentEvent(
                type="tool",
                message=f"Opening zero-download {mode} live network stream…",
                data={"tool": "resolve_live_stream", "mode": mode},
            )
            yield AgentEvent(
                type="stream",
                message="Live network stream ready",
                data={"url": plan.url, "mode": mode, "title": probe_out.title or plan.url},
            )
            if plan.action == "stream":
                yield AgentEvent(
                    type="answer",
                    message=f"Live {mode} stream connected for **{probe_out.title or plan.url}** (zero download).",
                    data={"probe": probe_out.model_dump(mode="json")},
                )
                yield AgentEvent(type="done", message="Complete")
                return

        # Helper to run transcript-grounded AI synthesis
        async def _synthesize() -> tuple[str, str, bool]:
            transcript = await self._fetch_live_transcript(probe_out)
            corpus = await self._build_corpus(probe_out, transcript)
            ans, llm_used = await summarize_text_with_llm(
                corpus,
                plan.summarize_question or request.prompt,
                settings=self._settings,
                byok_gemini_key=byok_gemini_key,
                output_style=plan.output_style,
            )
            return ans, llm_used, bool(transcript)

        # Tool 3: Summarize-only (or stream_and_summarize) with real transcript grounding!
        if plan.action in {"summarize", "stream_and_summarize"}:
            yield AgentEvent(
                type="tool",
                message="Pulling spoken captions & synthesizing AI brief…",
                data={"tool": "synthesize_brief"},
            )
            answer, llm_used, had_transcript = await _synthesize()
            if had_transcript:
                yield AgentEvent(
                    type="thought",
                    message="Grounded analysis in extracted video transcript captions.",
                    data={"transcript_grounded": True},
                )
            yield AgentEvent(
                type="answer",
                message=answer,
                data={"provider": llm_used, "transcript_grounded": had_transcript, "probe": probe_out.model_dump(mode="json")},
            )
            yield AgentEvent(type="done", message="Complete")
            return

        # Tool 4: Single or Multi-Step Extraction Pipeline
        step_specs = (
            plan.steps
            if (plan.action == "multi_step" and plan.steps)
            else [{"label": f"{plan.download_params.get('mode', 'auto')} extraction", "params": plan.download_params}]
        )

        completed_jobs: list[JobRecord] = []
        for idx, step in enumerate(step_specs[:3], start=1):
            step_label = str(step.get("label") or f"Step {idx}")
            step_params = step.get("params") if isinstance(step.get("params"), dict) else {}
            yield AgentEvent(
                type="tool",
                message=f"Step {idx}/{min(len(step_specs), 3)}: {step_label} via Neon Sync pipeline…",
                data={"tool": "extract_media", "step": idx, "label": step_label},
            )
            record, err_msg, err_data = await self._execute_single_job(
                url=plan.url,
                raw_params=step_params,
                client_key=client_key,
                pro_key=pro_key,
                byok_gemini_key=byok_gemini_key,
                account_tier=account_tier,
            )
            if record is None:
                yield AgentEvent(type="error", message=err_msg or "Extraction blocked", data=err_data or {})
                yield AgentEvent(type="done", message="Stopped")
                return

            yield AgentEvent(type="job", message="Job queued", data=record.model_dump(mode="json"))
            async for evt in self._job_manager.subscribe(record.job_id):
                yield AgentEvent(
                    type="progress",
                    message=evt.message or evt.stage,
                    data=evt.model_dump(mode="json"),
                )
                if evt.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                    break

            await asyncio.sleep(0.05)
            final_job = self._job_manager.get_job(record.job_id) or record
            yield AgentEvent(type="job", message=f"Job {final_job.status.value}", data=final_job.model_dump(mode="json"))
            if final_job.status == JobStatus.COMPLETED and final_job.result:
                completed_jobs.append(final_job)
            else:
                yield AgentEvent(type="error", message=final_job.error or f"Step '{step_label}' did not complete.")

        if completed_jobs:
            summary_lines = [
                f"• **{j.result.download.filename}** ({j.result.download.size_bytes or 0:,} bytes)"
                for j in completed_jobs
                if j.result and j.result.download.filename
            ]
            answer_msg = (
                f"Done! Extracted {len(completed_jobs)} artifact(s) via Neon Sync:\n" + "\n".join(summary_lines)
                if len(completed_jobs) > 1
                else f"Done! Extracted **{completed_jobs[0].result.download.filename}** "
                     f"({completed_jobs[0].result.download.size_bytes or 0:,} bytes) via Neon Sync."
            )
            if plan.action == "download_and_summarize" or (plan.action == "multi_step" and plan.summarize_question):
                yield AgentEvent(
                    type="tool",
                    message="Synthesizing transcript-grounded AI brief…",
                    data={"tool": "synthesize_brief"},
                )
                summary, _llm_used, _had_tr = await _synthesize()
                answer_msg = f"{answer_msg}\n\n{summary}"

            yield AgentEvent(
                type="answer",
                message=answer_msg,
                data={
                    "job": completed_jobs[0].model_dump(mode="json"),
                    "jobs": [j.model_dump(mode="json") for j in completed_jobs],
                },
            )
        else:
            yield AgentEvent(type="error", message="Extraction workflow did not produce any completed artifacts.")

        yield AgentEvent(type="done", message="Finished")
