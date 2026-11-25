"""Durable JobStore abstraction with InMemory and SQLite (WAL) implementations."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Protocol

from neon_sync.models import JobRecord, JobStatus

TERMINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


class JobStore(Protocol):
    """Persistence contract for JobRecords and idempotency keys (PostgreSQL/SQLite swappable)."""

    def save(self, record: JobRecord) -> None: ...
    def get(self, job_id: str) -> JobRecord | None: ...
    def list_recent(self, limit: int = 50) -> list[JobRecord]: ...
    def list_unfinished(self) -> list[JobRecord]: ...
    def get_idempotency(self, key: str) -> str | None: ...
    def set_idempotency(self, key: str, job_id: str) -> None: ...


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._idempotency: dict[str, str] = {}

    def save(self, record: JobRecord) -> None:
        self._jobs[record.job_id] = record

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def list_recent(self, limit: int = 50) -> list[JobRecord]:
        ordered = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return ordered[:limit]

    def get_idempotency(self, key: str) -> str | None:
        return self._idempotency.get(key)

    def list_unfinished(self) -> list[JobRecord]:
        return [job for job in self._jobs.values() if job.status not in TERMINAL_STATUSES]

    def set_idempotency(self, key: str, job_id: str) -> None:
        self._idempotency[key] = job_id


class SQLiteJobStore:
    """Zero-dependency durable SQLite job store (WAL mode) surviving process restarts."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path.resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                idem_key TEXT PRIMARY KEY,
                job_id TEXT NOT NULL
            );
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);"
        )
        self._conn.commit()

    def save(self, record: JobRecord) -> None:
        payload = record.model_dump_json()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs (job_id, created_at, status, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json
                """,
                (record.job_id, record.created_at.isoformat(), record.status.value, payload),
            )
            self._conn.commit()

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        return JobRecord.model_validate_json(row[0])

    def list_recent(self, limit: int = 50) -> list[JobRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [JobRecord.model_validate_json(r[0]) for r in rows]

    def get_idempotency(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id FROM idempotency_keys WHERE idem_key = ?",
                (key,),
            ).fetchone()
        return str(row[0]) if row else None

    def list_unfinished(self) -> list[JobRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM jobs WHERE status NOT IN (?, ?, ?)",
                tuple(status.value for status in TERMINAL_STATUSES),
            ).fetchall()
        return [JobRecord.model_validate_json(row[0]) for row in rows]

    def set_idempotency(self, key: str, job_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO idempotency_keys (idem_key, job_id) VALUES (?, ?)",
                (key, job_id),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
