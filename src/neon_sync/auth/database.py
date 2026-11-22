"""Durable SQLite (WAL) database for Neon Sync users, Google OAuth links, sessions, API keys & audit logs."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import ApiKeyPublic, AuthActivityEvent, AuthSessionPublic, UserPublic


@dataclass
class UserRow:
    id: str
    email: str
    email_verified: bool
    name: str
    avatar_url: str | None
    password_hash: str | None
    role: str
    tier: str
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None


@dataclass
class SessionRow:
    id: str
    user_id: str
    token_hash: str
    user_agent: str | None
    ip_address: str | None
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    revoked: bool


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    dt = datetime.fromisoformat(val)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class AuthDatabase:
    """Thread-safe SQLite (WAL) authentication database with strict schema & foreign keys."""

    def __init__(self, db_path: Path | str) -> None:
        self._is_memory = str(db_path) == ":memory:"
        if not self._is_memory:
            resolved = Path(db_path).resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            conn_target = str(resolved)
        else:
            conn_target = ":memory:"

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(conn_target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        if not self._is_memory:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    email_verified INTEGER NOT NULL DEFAULT 0,
                    name TEXT NOT NULL,
                    avatar_url TEXT,
                    password_hash TEXT,
                    role TEXT NOT NULL DEFAULT 'user',
                    tier TEXT NOT NULL DEFAULT 'free',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS oauth_accounts (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    provider_user_id TEXT NOT NULL,
                    provider_email TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(provider, provider_user_id)
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT UNIQUE NOT NULL,
                    user_agent TEXT,
                    ip_address TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_hash TEXT UNIQUE NOT NULL,
                    scopes TEXT NOT NULL DEFAULT 'read,write,agent',
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    code_verifier TEXT NOT NULL,
                    redirect_to TEXT NOT NULL DEFAULT '/',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_events (
                    id TEXT PRIMARY KEY,
                    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    event TEXT NOT NULL,
                    ip_address TEXT,
                    user_agent TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
                CREATE INDEX IF NOT EXISTS idx_oauth_user_id ON oauth_accounts(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
                CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
                CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
                CREATE INDEX IF NOT EXISTS idx_auth_events_user ON auth_events(user_id, created_at DESC);
                """
            )
            self._conn.commit()

    def _row_to_user(self, row: sqlite3.Row) -> UserRow:
        return UserRow(
            id=row["id"],
            email=row["email"],
            email_verified=bool(row["email_verified"]),
            name=row["name"],
            avatar_url=row["avatar_url"],
            password_hash=row["password_hash"],
            role=row["role"],
            tier=row["tier"],
            created_at=_parse_dt(row["created_at"]) or datetime.now(UTC),
            updated_at=_parse_dt(row["updated_at"]) or datetime.now(UTC),
            last_login_at=_parse_dt(row["last_login_at"]),
        )

    def to_public_user(self, row: UserRow) -> UserPublic:
        providers = self.list_user_providers(row.id)
        if row.password_hash:
            providers = ["password", *providers]
        return UserPublic(
            id=row.id,
            email=row.email,
            email_verified=row.email_verified,
            name=row.name,
            avatar_url=row.avatar_url,
            role="admin" if row.role == "admin" else "user",
            tier="pro" if row.tier == "pro" else "free",
            providers=providers,
            has_password=bool(row.password_hash),
            created_at=row.created_at,
            last_login_at=row.last_login_at,
        )

    # ---------- Users ----------
    def create_user(
        self,
        *,
        email: str,
        name: str,
        password_hash: str | None = None,
        avatar_url: str | None = None,
        email_verified: bool = False,
        tier: str = "free",
    ) -> UserRow:
        user_id = f"usr_{uuid.uuid4().hex[:16]}"
        now = _now_iso()
        clean_email = email.strip().lower()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO users (
                    id, email, email_verified, name, avatar_url, password_hash,
                    role, tier, created_at, updated_at, last_login_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?, ?, ?)
                """,
                (
                    user_id,
                    clean_email,
                    1 if email_verified else 0,
                    name.strip(),
                    avatar_url,
                    password_hash,
                    tier,
                    now,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row)

    def get_user_by_id(self, user_id: str) -> UserRow | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_email(self, email: str) -> UserRow | None:
        clean = email.strip().lower()
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE email = ?", (clean,)).fetchone()
        return self._row_to_user(row) if row else None

    def update_user_profile(
        self,
        user_id: str,
        *,
        name: str | None = None,
        avatar_url: str | None = None,
        email_verified: bool | None = None,
        tier: str | None = None,
        password_hash: str | None = None,
        touch_login: bool = False,
    ) -> UserRow | None:
        sets: list[str] = ["updated_at = ?"]
        params: list[object] = [_now_iso()]
        if name is not None:
            sets.append("name = ?")
            params.append(name.strip())
        if avatar_url is not None:
            sets.append("avatar_url = ?")
            params.append(avatar_url)
        if email_verified is not None:
            sets.append("email_verified = ?")
            params.append(1 if email_verified else 0)
        if tier is not None:
            sets.append("tier = ?")
            params.append(tier)
        if password_hash is not None:
            sets.append("password_hash = ?")
            params.append(password_hash)
        if touch_login:
            sets.append("last_login_at = ?")
            params.append(_now_iso())

        params.append(user_id)
        with self._lock:
            self._conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    # ---------- OAuth Accounts ----------
    def get_user_by_oauth(self, provider: str, provider_user_id: str) -> UserRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT u.* FROM users u
                JOIN oauth_accounts oa ON oa.user_id = u.id
                WHERE oa.provider = ? AND oa.provider_user_id = ?
                """,
                (provider, provider_user_id),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def link_oauth_account(
        self,
        *,
        user_id: str,
        provider: str,
        provider_user_id: str,
        provider_email: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO oauth_accounts (
                    id, user_id, provider, provider_user_id, provider_email, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"oau_{uuid.uuid4().hex[:16]}",
                    user_id,
                    provider,
                    provider_user_id,
                    provider_email.lower() if provider_email else None,
                    _now_iso(),
                ),
            )
            self._conn.commit()

    def list_user_providers(self, user_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT provider FROM oauth_accounts WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
        return [str(r["provider"]) for r in rows]

    # ---------- OAuth PKCE States ----------
    def save_oauth_state(
        self,
        *,
        state: str,
        code_verifier: str,
        redirect_to: str = "/",
        ttl_seconds: int = 600,
    ) -> None:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl_seconds)
        with self._lock:
            # Prune expired states opportunistically
            self._conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now.isoformat(),))
            self._conn.execute(
                """
                INSERT INTO oauth_states (state, code_verifier, redirect_to, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (state, code_verifier, redirect_to, now.isoformat(), expires.isoformat()),
            )
            self._conn.commit()

    def consume_oauth_state(self, state: str) -> tuple[str, str] | None:
        """Atomically fetch and delete a non-expired OAuth state, returning (code_verifier, redirect_to)."""
        now_iso = _now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT code_verifier, redirect_to, expires_at FROM oauth_states WHERE state = ?",
                (state,),
            ).fetchone()
            self._conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
            self._conn.commit()
        if not row:
            return None
        if row["expires_at"] < now_iso:
            return None
        return str(row["code_verifier"]), str(row["redirect_to"])

    # ---------- Sessions ----------
    def create_session(
        self,
        *,
        user_id: str,
        token_hash: str,
        ttl_seconds: int,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> SessionRow:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl_seconds)
        session_id = f"ses_{uuid.uuid4().hex[:16]}"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    id, user_id, token_hash, user_agent, ip_address,
                    created_at, expires_at, last_seen_at, revoked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    session_id,
                    user_id,
                    token_hash,
                    (user_agent or "")[:240] or None,
                    ip_address,
                    now.isoformat(),
                    expires.isoformat(),
                    now.isoformat(),
                ),
            )
            self._conn.commit()
        return SessionRow(
            id=session_id,
            user_id=user_id,
            token_hash=token_hash,
            user_agent=(user_agent or "")[:240] or None,
            ip_address=ip_address,
            created_at=now,
            expires_at=expires,
            last_seen_at=now,
            revoked=False,
        )

    def resolve_session(self, token_hash: str) -> tuple[SessionRow, UserRow] | None:
        now_iso = _now_iso()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    s.id AS s_id, s.user_id AS s_user_id, s.token_hash AS s_token_hash,
                    s.user_agent AS s_user_agent, s.ip_address AS s_ip_address,
                    s.created_at AS s_created_at, s.expires_at AS s_expires_at,
                    s.last_seen_at AS s_last_seen_at, s.revoked AS s_revoked,
                    u.*
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.revoked = 0 AND s.expires_at > ?
                """,
                (token_hash, now_iso),
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
                (now_iso, row["s_id"]),
            )
            self._conn.commit()

        sess = SessionRow(
            id=row["s_id"],
            user_id=row["s_user_id"],
            token_hash=row["s_token_hash"],
            user_agent=row["s_user_agent"],
            ip_address=row["s_ip_address"],
            created_at=_parse_dt(row["s_created_at"]) or datetime.now(UTC),
            expires_at=_parse_dt(row["s_expires_at"]) or datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
            revoked=bool(row["s_revoked"]),
        )
        return sess, self._row_to_user(row)

    def list_user_sessions(
        self,
        user_id: str,
        *,
        current_session_id: str | None = None,
    ) -> list[AuthSessionPublic]:
        now_iso = _now_iso()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, user_agent, ip_address, created_at, last_seen_at, expires_at
                FROM sessions
                WHERE user_id = ? AND revoked = 0 AND expires_at > ?
                ORDER BY last_seen_at DESC
                """,
                (user_id, now_iso),
            ).fetchall()
        return [
            AuthSessionPublic(
                id=r["id"],
                user_agent=r["user_agent"],
                ip_address=r["ip_address"],
                created_at=_parse_dt(r["created_at"]) or datetime.now(UTC),
                last_seen_at=_parse_dt(r["last_seen_at"]) or datetime.now(UTC),
                expires_at=_parse_dt(r["expires_at"]) or datetime.now(UTC),
                is_current=(r["id"] == current_session_id),
            )
            for r in rows
        ]

    def revoke_session(self, session_id: str, *, user_id: str | None = None) -> bool:
        with self._lock:
            if user_id:
                cur = self._conn.execute(
                    "UPDATE sessions SET revoked = 1 WHERE id = ? AND user_id = ?",
                    (session_id, user_id),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE sessions SET revoked = 1 WHERE id = ?",
                    (session_id,),
                )
            self._conn.commit()
            return cur.rowcount > 0

    def revoke_other_sessions(self, user_id: str, *, keep_session_id: str | None) -> int:
        with self._lock:
            if keep_session_id:
                cur = self._conn.execute(
                    "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND id != ? AND revoked = 0",
                    (user_id, keep_session_id),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE sessions SET revoked = 1 WHERE user_id = ? AND revoked = 0",
                    (user_id,),
                )
            self._conn.commit()
            return cur.rowcount

    # ---------- Developer API Keys ----------
    def create_api_key(
        self,
        *,
        user_id: str,
        name: str,
        key_prefix: str,
        key_hash: str,
        scopes: list[str],
    ) -> ApiKeyPublic:
        key_id = f"key_{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)
        scopes_str = ",".join(scopes)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO api_keys (
                    id, user_id, name, key_prefix, key_hash, scopes, created_at, revoked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (key_id, user_id, name.strip(), key_prefix, key_hash, scopes_str, now.isoformat()),
            )
            self._conn.commit()
        return ApiKeyPublic(
            id=key_id,
            name=name.strip(),
            key_prefix=key_prefix,
            scopes=list(scopes),
            created_at=now,
            last_used_at=None,
            revoked=False,
        )

    def resolve_api_key(self, key_hash: str) -> tuple[str, UserRow] | None:
        now_iso = _now_iso()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT k.id AS k_id, u.*
                FROM api_keys k
                JOIN users u ON u.id = k.user_id
                WHERE k.key_hash = ? AND k.revoked = 0
                """,
                (key_hash,),
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                (now_iso, row["k_id"]),
            )
            self._conn.commit()
        return str(row["k_id"]), self._row_to_user(row)

    def list_api_keys(self, user_id: str) -> list[ApiKeyPublic]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, name, key_prefix, scopes, created_at, last_used_at, revoked
                FROM api_keys
                WHERE user_id = ? AND revoked = 0
                ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [
            ApiKeyPublic(
                id=r["id"],
                name=r["name"],
                key_prefix=r["key_prefix"],
                scopes=[s for s in str(r["scopes"]).split(",") if s],
                created_at=_parse_dt(r["created_at"]) or datetime.now(UTC),
                last_used_at=_parse_dt(r["last_used_at"]),
                revoked=bool(r["revoked"]),
            )
            for r in rows
        ]

    def revoke_api_key(self, key_id: str, *, user_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_keys SET revoked = 1 WHERE id = ? AND user_id = ? AND revoked = 0",
                (key_id, user_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- Security Audit Log ----------
    def record_event(
        self,
        *,
        user_id: str | None,
        event: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO auth_events (id, user_id, event, ip_address, user_agent, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"evt_{uuid.uuid4().hex[:14]}",
                    user_id,
                    event,
                    ip_address,
                    (user_agent or "")[:240] or None,
                    detail,
                    _now_iso(),
                ),
            )
            self._conn.commit()

    def list_events(self, user_id: str, *, limit: int = 30) -> list[AuthActivityEvent]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, event, ip_address, user_agent, detail, created_at
                FROM auth_events
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, max(1, min(limit, 100))),
            ).fetchall()
        return [
            AuthActivityEvent(
                id=r["id"],
                event=r["event"],
                ip_address=r["ip_address"],
                user_agent=r["user_agent"],
                detail=r["detail"],
                created_at=_parse_dt(r["created_at"]) or datetime.now(UTC),
            )
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
