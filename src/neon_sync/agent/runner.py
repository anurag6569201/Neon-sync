"""Neon Sync AI Agent SSE Execution Runner."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, Field

from neon_sync.adapters import AdapterError
from neon_sync.config import Settings
from neon_sync.models import DownloadRequest, JobStatus, ProbeRequest
from neon_sync.pipeline import ExtractionOrchestrator, JobManager
from neon_sync.pipeline.tiers import TierLimitError, TierManager
from neon_sync.security import UrlValidationError, validate_and_canonicalize_url

from .planner import AgentRequest, plan_agent_action, summarize_text_with_llm


class AgentEvent(BaseModel):
    type: Literal["thought", "plan", "tool", "progress", "answer", "job", "error", "done"]
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class NeonSyncAgent:
    """Executes natural-language media workflows over Neon Sync's safe internal services."""

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

    async def run_stream(
        self,
        request: AgentRequest,
        *,
        client_key: str,
        pro_key: str | None = None,
        byok_gemini_key: str | None = None,
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
                "download_params": plan.download_params,
            },
        )

        # Tool 1: Always inspect metadata cleanly first
        yield AgentEvent(type="tool", message="Inspecting source metadata via Engine Registry…", data={"tool": "probe_url"})
        try:
            probe_out = await self._orchestrator.probe(ProbeRequest(url=plan.url))
        except (AdapterError, UrlValidationError) as exc:
            yield AgentEvent(type="error", message=str(exc))
            yield AgentEvent(type="done", message="Failed")
            return

        yield AgentEvent(
            type="thought",
            message=f"Detected {probe_out.content_type.value}: “{probe_out.title or probe_out.url}” via {probe_out.engine.engine_used}.",
            data={
                "title": probe_out.title,
                "content_type": probe_out.content_type.value,
                "engine": probe_out.engine.engine_used,
                "formats": len(probe_out.available_formats),
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

        # Tool 2: Summarize-only (using description/metadata + optional captions)
        if plan.action == "summarize":
            yield AgentEvent(
                type="tool",
                message="Synthesizing media summary from metadata & captions…",
                data={"tool": "summarize_media"},
            )
            corpus_parts = [
                f"Title: {probe_out.title or 'Unknown'}",
                f"Source: {probe_out.source.domain}",
                f"Author: {probe_out.author.name if probe_out.author else 'Unknown'}",
                f"Duration: {probe_out.duration_seconds}s" if probe_out.duration_seconds else "",
                f"Description:\n{probe_out.description or 'No description provided.'}",
            ]
            answer, llm_used = await summarize_text_with_llm(
                "\n".join(p for p in corpus_parts if p),
                plan.summarize_question or request.prompt,
                settings=self._settings,
                byok_gemini_key=byok_gemini_key,
            )
            yield AgentEvent(
                type="answer",
                message=answer,
                data={"provider": llm_used, "probe": probe_out.model_dump(mode="json")},
            )
            yield AgentEvent(type="done", message="Complete")
            return

        # Tool 3: Download / Clip / Convert execution
        dl_kwargs = {k: v for k, v in plan.download_params.items() if v is not None}
        try:
            dl_req = DownloadRequest(url=plan.url, **dl_kwargs)
            self._tier_manager.check_and_consume_download(
                client_key,
                dl_req,
                pro_key=pro_key,
                byok_key=byok_gemini_key,
            )
        except TierLimitError as exc:
            yield AgentEvent(
                type="error",
                message=exc.message,
                data={"code": exc.code, "upgrade_hint": exc.upgrade_hint},
            )
            yield AgentEvent(type="done", message="Upgrade required")
            return
        except Exception as exc:  # noqa: BLE001
            yield AgentEvent(type="error", message=f"Invalid extraction plan: {exc}")
            yield AgentEvent(type="done", message="Failed")
            return

        yield AgentEvent(
            type="tool",
            message=f"Executing extraction ({dl_req.mode}) via Neon Sync pipeline…",
            data={"tool": "execute_download", "params": dl_req.model_dump(mode="json")},
        )
        record = await self._job_manager.submit(dl_req)
        yield AgentEvent(type="job", message="Job queued", data=record.model_dump(mode="json"))

        # Stream live job progress events straight through the Agent SSE stream
        async for evt in self._job_manager.subscribe(record.job_id):
            yield AgentEvent(
                type="progress",
                message=evt.message or evt.stage,
                data=evt.model_dump(mode="json"),
            )
            if evt.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                break

        # Brief yield to ensure JobManager finalized stored result
        await asyncio.sleep(0.05)
        final_job = self._job_manager.get_job(record.job_id) or record
        yield AgentEvent(type="job", message=f"Job {final_job.status.value}", data=final_job.model_dump(mode="json"))

        if final_job.status == JobStatus.COMPLETED and final_job.result:
            dl = final_job.result.download
            answer_msg = f"Done! Extracted **{dl.filename}** ({dl.size_bytes or 0:,} bytes) via `{final_job.result.engine.engine_used}`."
            if plan.action == "download_and_summarize":
                summary, llm_used = await summarize_text_with_llm(
                    f"Title: {final_job.result.title}\nDescription: {final_job.result.description or ''}",
                    plan.summarize_question or request.prompt,
                    settings=self._settings,
                    byok_gemini_key=byok_gemini_key,
                )
                answer_msg = f"{answer_msg}\n\n{summary}"
            yield AgentEvent(
                type="answer",
                message=answer_msg,
                data={"job": final_job.model_dump(mode="json")},
            )
        else:
            yield AgentEvent(type="error", message=final_job.error or "Extraction did not complete.")

        yield AgentEvent(type="done", message="Finished")
