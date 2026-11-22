"""Durable SQLite Automations Store, Async Scheduler & SSRF-Safe HMAC Webhook Dispatcher."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx

from neon_sync.agent import AgentRequest, NeonSyncAgent
from neon_sync.config import Settings
from neon_sync.models import DownloadRequest
from neon_sync.models.options import PRESETS
from neon_sync.pipeline import ExtractionOrchestrator, JobManager
from neon_sync.security import validate_and_canonicalize_url

from .models import (
    AutomationCreateRequest,
    AutomationRule,
    AutomationRunRecord,
    AutomationTemplate,
    AutomationUpdateRequest,
)

log = logging.getLogger("neon_sync.automation")

AUTOMATION_TEMPLATES: list[AutomationTemplate] = [
    AutomationTemplate(
        id="tpl_podcast_master",
        name="Podcast Audio Master + Show Notes",
        description="Extracts 320k loudness-normalized MP3 audio, pulls spoken captions, and synthesizes AI show notes.",
        action_type="agent_prompt",
        trigger_type="manual",
        prompt="extract audio as 320k mp3 with loudnorm and summarize key takeaways with timestamps",
        badge="AUDIO + AI",
    ),
    AutomationTemplate(
        id="tpl_ai_digest",
        name="Hourly AI Research Digest",
        description="Inspects a media URL on schedule, grounds analysis in real captions via Gemini/Cloudflare AI, and posts to your webhook.",
        action_type="ai_brief",
        trigger_type="interval",
        interval_minutes=60,
        prompt="summarize the core insights, key points, and action items",
        badge="SCHEDULED",
    ),
    AutomationTemplate(
        id="tpl_reel_kit",
        name="Social Reel & Audio Creator Kit",
        description="Runs a multi-step agent workflow producing a 1080p H.264 MP4, 320k MP3 master, and viral social hooks.",
        action_type="agent_prompt",
        trigger_type="manual",
        prompt="create a viral reel kit with 1080p mp4, 320k mp3 and social hooks",
        badge="MULTI-STEP",
    ),
    AutomationTemplate(
        id="tpl_fast_1080p_archive",
        name="1080p H.264 MP4 Archive Sync",
        description="Automatically captures a target link as a universal 1080p H.264 MP4 with faststart & metadata.",
        action_type="extract_preset",
        trigger_type="interval",
        interval_minutes=360,
        preset_id="best_compatible_mp4",
        badge="ARCHIVE",
    ),
]


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    dt = datetime.fromisoformat(val)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class AutomationStore:
    """Thread-safe SQLite (WAL) store for automation rules and run histories."""

    def __init__(self, db_path: Path | str) -> None:
        self._is_memory = str(db_path) == ":memory:"
        if not self._is_memory:
            resolved = Path(db_path).resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            target = str(resolved)
        else:
            target = ":memory:"

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        if not self._is_memory:
            self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS automations (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                name TEXT NOT NULL,
                target_url TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                interval_minutes INTEGER,
                action_type TEXT NOT NULL,
                prompt TEXT,
                preset_id TEXT,
                webhook_url TEXT,
                webhook_secret TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                run_count INTEGER NOT NULL DEFAULT 0,
                last_status TEXT,
                last_summary TEXT,
                last_run_at TEXT,
                next_run_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS automation_runs (
                id TEXT PRIMARY KEY,
                automation_id TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                summary TEXT NOT NULL DEFAULT '',
                job_ids_json TEXT NOT NULL DEFAULT '[]',
                webhook_status TEXT,
                error TEXT,
                output_preview_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_auto_next_run ON automations(enabled, next_run_at);
            CREATE INDEX IF NOT EXISTS idx_auto_runs ON automation_runs(automation_id, started_at DESC);
            """
        )
        self._conn.commit()

    def _row_to_rule(self, r: sqlite3.Row) -> AutomationRule:
        return AutomationRule(
            id=r["id"],
            owner_id=r["owner_id"],
            name=r["name"],
            target_url=r["target_url"],
            trigger_type=r["trigger_type"],
            interval_minutes=r["interval_minutes"],
            action_type=r["action_type"],
            prompt=r["prompt"],
            preset_id=r["preset_id"],
            webhook_url=r["webhook_url"],
            enabled=bool(r["enabled"]),
            run_count=int(r["run_count"]),
            last_status=r["last_status"],
            last_summary=r["last_summary"],
            last_run_at=_parse_dt(r["last_run_at"]),
            next_run_at=_parse_dt(r["next_run_at"]),
            created_at=_parse_dt(r["created_at"]) or datetime.now(UTC),
        )

    def _row_to_run(self, r: sqlite3.Row) -> AutomationRunRecord:
        return AutomationRunRecord(
            id=r["id"],
            automation_id=r["automation_id"],
            trigger=r["trigger"],
            status=r["status"],
            started_at=_parse_dt(r["started_at"]) or datetime.now(UTC),
            completed_at=_parse_dt(r["completed_at"]),
            summary=r["summary"] or "",
            job_ids=json.loads(r["job_ids_json"] or "[]"),
            webhook_status=r["webhook_status"],
            error=r["error"],
            output_preview=json.loads(r["output_preview_json"] or "{}"),
        )

    def create(self, req: AutomationCreateRequest, *, owner_id: str) -> AutomationRule:
        aid = f"aut_{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)
        next_run = (
            (now + timedelta(minutes=req.interval_minutes)).isoformat()
            if (req.trigger_type == "interval" and req.interval_minutes and req.enabled)
            else None
        )
        secret = f"whsec_{secrets.token_urlsafe(24)}"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO automations (
                    id, owner_id, name, target_url, trigger_type, interval_minutes,
                    action_type, prompt, preset_id, webhook_url, webhook_secret,
                    enabled, run_count, next_run_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    aid,
                    owner_id,
                    req.name.strip(),
                    req.target_url.strip(),
                    req.trigger_type,
                    req.interval_minutes,
                    req.action_type,
                    req.prompt.strip() if req.prompt else None,
                    req.preset_id,
                    req.webhook_url.strip() if req.webhook_url else None,
                    secret,
                    1 if req.enabled else 0,
                    next_run,
                    now.isoformat(),
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM automations WHERE id = ?", (aid,)).fetchone()
        return self._row_to_rule(row)

    def get(self, automation_id: str) -> tuple[AutomationRule, str] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM automations WHERE id = ?", (automation_id,)).fetchone()
        if not row:
            return None
        return self._row_to_rule(row), str(row["webhook_secret"])

    def list_all(self, *, owner_id: str | None = None) -> list[AutomationRule]:
        with self._lock:
            if owner_id:
                rows = self._conn.execute(
                    "SELECT * FROM automations WHERE owner_id IN (?, 'workspace') ORDER BY created_at DESC",
                    (owner_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM automations ORDER BY created_at DESC"
                ).fetchall()
        return [self._row_to_rule(r) for r in rows]

    def update(self, automation_id: str, req: AutomationUpdateRequest) -> AutomationRule | None:
        existing = self.get(automation_id)
        if existing is None:
            return None
        rule, _ = existing
        enabled = rule.enabled if req.enabled is None else req.enabled
        interval = rule.interval_minutes if req.interval_minutes is None else req.interval_minutes
        next_run = (
            (datetime.now(UTC) + timedelta(minutes=interval)).isoformat()
            if (rule.trigger_type == "interval" and interval and enabled)
            else None
        )
        with self._lock:
            self._conn.execute(
                """
                UPDATE automations
                SET name = ?, enabled = ?, interval_minutes = ?, prompt = ?, webhook_url = ?, next_run_at = ?
                WHERE id = ?
                """,
                (
                    req.name.strip() if req.name is not None else rule.name,
                    1 if enabled else 0,
                    interval,
                    req.prompt.strip() if req.prompt is not None else rule.prompt,
                    req.webhook_url.strip() if req.webhook_url is not None else rule.webhook_url,
                    next_run,
                    automation_id,
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM automations WHERE id = ?", (automation_id,)).fetchone()
        return self._row_to_rule(row) if row else None

    def delete(self, automation_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM automations WHERE id = ?", (automation_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def list_due(self) -> list[AutomationRule]:
        now_iso = datetime.now(UTC).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM automations
                WHERE enabled = 1 AND trigger_type = 'interval'
                  AND next_run_at IS NOT NULL AND next_run_at <= ?
                """,
                (now_iso,),
            ).fetchall()
        return [self._row_to_rule(r) for r in rows]

    def save_run(self, run: AutomationRunRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO automation_runs (
                    id, automation_id, trigger, status, started_at, completed_at,
                    summary, job_ids_json, webhook_status, error, output_preview_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    completed_at = excluded.completed_at,
                    summary = excluded.summary,
                    job_ids_json = excluded.job_ids_json,
                    webhook_status = excluded.webhook_status,
                    error = excluded.error,
                    output_preview_json = excluded.output_preview_json
                """,
                (
                    run.id,
                    run.automation_id,
                    run.trigger,
                    run.status,
                    run.started_at.isoformat(),
                    run.completed_at.isoformat() if run.completed_at else None,
                    run.summary,
                    json.dumps(run.job_ids),
                    run.webhook_status,
                    run.error,
                    json.dumps(run.output_preview),
                ),
            )
            self._conn.commit()

    def mark_rule_ran(
        self,
        rule: AutomationRule,
        *,
        status: str,
        summary: str,
    ) -> None:
        now = datetime.now(UTC)
        next_run = (
            (now + timedelta(minutes=rule.interval_minutes)).isoformat()
            if (rule.trigger_type == "interval" and rule.interval_minutes and rule.enabled)
            else None
        )
        with self._lock:
            self._conn.execute(
                """
                UPDATE automations
                SET run_count = run_count + 1,
                    last_status = ?,
                    last_summary = ?,
                    last_run_at = ?,
                    next_run_at = ?
                WHERE id = ?
                """,
                (status, summary[:400], now.isoformat(), next_run, rule.id),
            )
            self._conn.commit()

    def list_runs(self, automation_id: str, *, limit: int = 20) -> list[AutomationRunRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM automation_runs
                WHERE automation_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (automation_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class AutomationEngine:
    """Executes Neon Sync automation rules, background interval schedules, and HMAC webhooks."""

    def __init__(
        self,
        settings: Settings,
        store: AutomationStore,
        orchestrator: ExtractionOrchestrator,
        job_manager: JobManager,
        agent: NeonSyncAgent,
    ) -> None:
        self._settings = settings
        self._store = store
        self._orchestrator = orchestrator
        self._job_manager = job_manager
        self._agent = agent
        self._scheduler_task: asyncio.Task[None] | None = None

    @property
    def store(self) -> AutomationStore:
        return self._store

    def start_scheduler(self) -> None:
        if not self._settings.enable_automation_scheduler:
            return
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="neon-sync-automations")

    async def stop_scheduler(self) -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                for rule in self._store.list_due():
                    try:
                        await self.execute_rule(rule.id, trigger="scheduled")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Scheduled automation %s failed: %s", rule.id, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Automation scheduler tick error: %s", exc)
            await asyncio.sleep(30.0)

    async def _dispatch_webhook(
        self,
        *,
        webhook_url: str,
        webhook_secret: str,
        payload: dict[str, Any],
    ) -> str:
        """SSRF-safe, HMAC-SHA256 signed JSON webhook delivery."""
        try:
            validated = validate_and_canonicalize_url(
                webhook_url,
                allow_private=self._settings.allow_private_addresses,
                max_length=self._settings.max_url_length,
            )
            body_bytes = json.dumps(payload, default=str).encode("utf-8")
            sig = hmac.new(webhook_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
            client_kwargs: dict[str, Any] = {"timeout": 12.0, "follow_redirects": False}
            if self._settings.egress_proxy_url:
                client_kwargs["proxy"] = self._settings.egress_proxy_url
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.post(
                    validated.canonical_url,
                    content=body_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Neon-Sync-Automations/1.0",
                        "X-Neon-Sync-Event": "automation.run.completed",
                        "X-Neon-Sync-Signature": f"sha256={sig}",
                    },
                )
            return f"http_{resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"error:{str(exc)[:80]}"

    async def execute_rule(
        self,
        automation_id: str,
        *,
        trigger: Literal["manual", "scheduled", "webhook"] = "manual",
        client_key: str = "automation",
        pro_key: str | None = None,
        byok_gemini_key: str | None = None,
        account_tier: str | None = "pro",
    ) -> AutomationRunRecord:
        found = self._store.get(automation_id)
        if found is None:
            raise KeyError("Automation not found")
        rule, webhook_secret = found

        run = AutomationRunRecord(
            id=f"run_{uuid.uuid4().hex[:12]}",
            automation_id=rule.id,
            trigger=trigger,
            status="running",
            started_at=datetime.now(UTC),
        )
        self._store.save_run(run)

        try:
            if rule.action_type == "extract_preset":
                preset = next((p for p in PRESETS if p.id == rule.preset_id), PRESETS[0])
                dl_req = DownloadRequest(url=rule.target_url, **preset.settings)
                job = await self._job_manager.submit(dl_req)
                run.job_ids = [job.job_id]
                run.summary = f"Queued preset '{preset.name}' ({job.job_id})"
                run.output_preview = {"job_id": job.job_id, "preset": preset.id, "url": rule.target_url}
            else:
                prompt_text = rule.prompt or (
                    "summarize the key points and insights" if rule.action_type == "ai_brief" else "extract best quality"
                )
                req = AgentRequest(prompt=prompt_text, url=rule.target_url)
                final_answer = ""
                collected_jobs: list[str] = []
                async for ev in self._agent.run_stream(
                    req,
                    client_key=client_key,
                    pro_key=pro_key,
                    byok_gemini_key=byok_gemini_key,
                    account_tier=account_tier,
                ):
                    if ev.type == "job" and isinstance(ev.data.get("job_id"), str):
                        jid = ev.data["job_id"]
                        if jid not in collected_jobs:
                            collected_jobs.append(jid)
                    elif ev.type == "answer":
                        final_answer = ev.message
                    elif ev.type == "error":
                        raise RuntimeError(ev.message)
                run.job_ids = collected_jobs
                run.summary = final_answer[:380] if final_answer else f"Completed automation '{rule.name}'"
                run.output_preview = {"answer": final_answer[:1200], "job_ids": collected_jobs}

            run.status = "completed"
            run.completed_at = datetime.now(UTC)

            if rule.webhook_url:
                run.webhook_status = await self._dispatch_webhook(
                    webhook_url=rule.webhook_url,
                    webhook_secret=webhook_secret,
                    payload={
                        "service": "neon-sync",
                        "event": "automation.run.completed",
                        "automation_id": rule.id,
                        "automation_name": rule.name,
                        "run_id": run.id,
                        "status": run.status,
                        "summary": run.summary,
                        "job_ids": run.job_ids,
                        "completed_at": run.completed_at.isoformat(),
                    },
                )

            self._store.save_run(run)
            self._store.mark_rule_ran(rule, status="completed", summary=run.summary)
            return run
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.error = str(exc)
            run.summary = f"Failed: {exc}"
            run.completed_at = datetime.now(UTC)
            self._store.save_run(run)
            self._store.mark_rule_ran(rule, status="failed", summary=run.summary)
            return run
