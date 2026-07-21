"""Session management with optional SQLite persistence.

Provides two storage backends:
- ``MemoryStore``: in-memory, for development and testing.
- ``SqliteStore``: durable, for production deployments.

``SessionManager`` is the public facade used by the rest of the application;
it selects the backend based on the configured ``db_path`` and manages
per-session asyncio locks for in-process mutual exclusion.
"""

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

import aiosqlite

from app.models import AnalysisReport, IssueData, SessionStatus

logger = logging.getLogger(__name__)


class SessionConflictError(RuntimeError):
    """Raised when another process updated a session first."""


@dataclass
class Session:
    session_id: str
    issue_url: str
    issue: IssueData | None = None
    tree: list[str] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    file_cache: dict[str, str] = field(default_factory=dict)
    files_read: list[str] = field(default_factory=list)
    report: AnalysisReport | None = None
    pending_pr: dict | None = None
    display_title: str | None = None
    status: SessionStatus = "queued"
    phase: str = "queued"
    version: int = 0
    metrics: dict[str, int | float] = field(default_factory=dict)
    cancel_requested: bool = False
    error_message: str | None = None
    archived_at: str | None = None
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)


class MemoryStore:
    """In-memory session store (dev / testing)."""

    def __init__(self, max_sessions: int = 100) -> None:
        self._sessions: dict[str, Session] = {}
        self._events: dict[str, list[dict]] = {}
        self._max = max_sessions

    async def create(self, issue_url: str) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(session_id=sid, issue_url=issue_url)
        self._sessions[sid] = session
        self._events[sid] = []
        self._evict()
        return session

    async def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def save(self, session: Session) -> None:
        session.updated_at = _now()
        session.version += 1

    async def append_event(self, session_id: str, event: dict) -> dict:
        records = self._events.setdefault(session_id, [])
        record = {**event, "sequence": len(records) + 1, "created_at": _now()}
        records.append(record)
        return record

    async def list_events(self, session_id: str) -> list[dict]:
        return list(self._events.get(session_id, []))

    async def request_cancel(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None or session.status != "running":
            return False
        session.cancel_requested = True
        session.updated_at = _now()
        session.version += 1
        return True

    async def is_cancel_requested(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        return bool(session and session.cancel_requested)

    async def recover_stale(self, cutoff: str) -> int:
        recovered = 0
        for session in self._sessions.values():
            if session.status == "running" and session.updated_at < cutoff:
                session.status = "failed"
                session.phase = "interrupted"
                session.error_message = "Investigation was interrupted before completion"
                session.updated_at = _now()
                session.version += 1
                recovered += 1
        return recovered

    async def save_pr_proposal(self, session_id: str, proposal: dict) -> None:
        if session := self._sessions.get(session_id):
            session.pending_pr = proposal

    async def get_pr_proposal(self, session_id: str) -> dict | None:
        if session := self._sessions.get(session_id):
            return session.pending_pr
        return None

    async def delete_pr_proposal(self, session_id: str) -> None:
        if session := self._sessions.get(session_id):
            session.pending_pr = None

    async def list(self, *, archived: bool, query: str, limit: int) -> list[Session]:
        normalized_query = query.casefold().strip()
        sessions = [
            session
            for session in self._sessions.values()
            if (session.archived_at is not None) == archived
            and (not normalized_query or normalized_query in _session_search_text(session))
        ]
        return sorted(sessions, key=lambda session: session.updated_at, reverse=True)[:limit]

    async def delete(self, session_id: str) -> bool:
        self._events.pop(session_id, None)
        return self._sessions.pop(session_id, None) is not None

    async def update_metrics(self, session_id: str, metrics: dict) -> None:
        """MemoryStore 中 metrics 直接写回 session 对象（若存在）。"""
        if session := self._sessions.get(session_id):
            session.metrics = dict(metrics)

    def _evict(self) -> None:
        while len(self._sessions) > self._max:
            oldest = next(iter(self._sessions))
            del self._sessions[oldest]
            self._events.pop(oldest, None)


class SqliteStore:
    """SQLite-backed session store (production)."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            from app.db import get_db

            self._conn = await get_db(self._path)
        return self._conn

    async def create(self, issue_url: str) -> Session:
        sid = uuid.uuid4().hex[:12]
        now = _now()
        db = await self._get_conn()
        await db.execute(
            "INSERT INTO sessions (session_id, issue_url, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (sid, issue_url, now, now),
        )
        await db.commit()
        return Session(session_id=sid, issue_url=issue_url, created_at=now, updated_at=now)

    async def get(self, session_id: str) -> Session | None:
        db = await self._get_conn()
        row = await (await db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))).fetchone()
        if row is None:
            return None
        return _row_to_session(row)

    async def save(self, session: Session) -> None:
        db = await self._get_conn()
        session.updated_at = _now()
        cursor = await db.execute(
            """UPDATE sessions SET issue_json=?, tree_json=?, messages_json=?, file_cache_json=?,
               files_read_json=?, report_json=?, display_title=?, status=?, phase=?, metrics_json=?,
               cancel_requested=?, error_message=?, archived_at=?, updated_at=?, version=version+1
               WHERE session_id=? AND version=?""",
            (
                session.issue.model_dump_json() if session.issue else None,
                json.dumps(session.tree, ensure_ascii=False),
                json.dumps(session.messages, ensure_ascii=False, default=str),
                json.dumps(session.file_cache, ensure_ascii=False),
                json.dumps(session.files_read, ensure_ascii=False),
                session.report.model_dump_json() if session.report else None,
                session.display_title,
                session.status,
                session.phase,
                json.dumps(session.metrics, ensure_ascii=False),
                int(session.cancel_requested),
                session.error_message,
                session.archived_at,
                session.updated_at,
                session.session_id,
                session.version,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise SessionConflictError(f"Session {session.session_id} was updated concurrently")
        await db.commit()
        session.version += 1

    async def append_event(self, session_id: str, event: dict) -> dict:
        db = await self._get_conn()
        cursor = await db.execute(
            "INSERT INTO session_events (session_id, event_type, data_json, message, created_at) VALUES (?,?,?,?,?)",
            (
                session_id,
                event["type"],
                (
                    json.dumps(event.get("data"), ensure_ascii=False, default=str)
                    if event.get("data") is not None
                    else None
                ),
                event.get("message", ""),
                _now(),
            ),
        )
        await db.commit()
        return {
            **event,
            "sequence": cursor.lastrowid,
            "created_at": _now(),
        }

    async def list_events(self, session_id: str) -> list[dict]:
        db = await self._get_conn()
        rows = await (
            await db.execute(
                "SELECT id, event_type, data_json, message, created_at FROM session_events "
                "WHERE session_id=? ORDER BY id",
                (session_id,),
            )
        ).fetchall()
        return [
            {
                "sequence": row["id"],
                "type": row["event_type"],
                "data": json.loads(row["data_json"]) if row["data_json"] else None,
                "message": row["message"] or "",
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def request_cancel(self, session_id: str) -> bool:
        db = await self._get_conn()
        cursor = await db.execute(
            "UPDATE sessions SET cancel_requested=1, updated_at=?, version=version+1 "
            "WHERE session_id=? AND status='running'",
            (_now(), session_id),
        )
        await db.commit()
        return cursor.rowcount == 1

    async def is_cancel_requested(self, session_id: str) -> bool:
        db = await self._get_conn()
        row = await (
            await db.execute("SELECT cancel_requested FROM sessions WHERE session_id=?", (session_id,))
        ).fetchone()
        return bool(row and row["cancel_requested"])

    async def recover_stale(self, cutoff: str) -> int:
        db = await self._get_conn()
        cursor = await db.execute(
            """UPDATE sessions
               SET status='failed', phase='interrupted',
                   error_message='Investigation was interrupted before completion',
                   updated_at=?, version=version+1
               WHERE status='running' AND updated_at < ?""",
            (_now(), cutoff),
        )
        await db.commit()
        return cursor.rowcount

    async def save_pr_proposal(self, session_id: str, proposal: dict) -> None:
        db = await self._get_conn()
        await db.execute(
            "INSERT OR REPLACE INTO pending_pr (session_id, branch, title, body, changes_json) VALUES (?,?,?,?,?)",
            (
                session_id,
                proposal["branch"],
                proposal["title"],
                proposal["body"],
                json.dumps(proposal.get("changes", []), ensure_ascii=False),
            ),
        )
        await db.commit()

    async def get_pr_proposal(self, session_id: str) -> dict | None:
        db = await self._get_conn()
        row = await (await db.execute("SELECT * FROM pending_pr WHERE session_id = ?", (session_id,))).fetchone()
        if row is None:
            return None
        return {
            "branch": row["branch"],
            "title": row["title"],
            "body": row["body"],
            "changes": json.loads(row["changes_json"]),
        }

    async def delete_pr_proposal(self, session_id: str) -> None:
        db = await self._get_conn()
        await db.execute("DELETE FROM pending_pr WHERE session_id = ?", (session_id,))
        await db.commit()

    async def list(self, *, archived: bool, query: str, limit: int) -> list[Session]:
        db = await self._get_conn()
        clauses: list[str] = []
        params: list[object] = []
        clauses.append("archived_at IS NOT NULL" if archived else "archived_at IS NULL")
        if query.strip():
            pattern = f"%{query.strip()}%"
            clauses.append("(issue_url LIKE ? OR display_title LIKE ? OR issue_json LIKE ?)")
            params.extend([pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = await (
            await db.execute(
                f"SELECT * FROM sessions {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            )
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    async def delete(self, session_id: str) -> bool:
        db = await self._get_conn()
        await db.execute("DELETE FROM pending_pr WHERE session_id = ?", (session_id,))
        cursor = await db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        await db.commit()
        return cursor.rowcount > 0

    async def update_metrics(self, session_id: str, metrics: dict) -> None:
        """轻量更新 metrics 列，不触碰 version 也不写完整 session。

        实时调查过程中工具调用频繁，每次走完整 save() 会触发乐观锁版本递增
        并可能与 stream 端点的 save 竞争。此方法只更新 metrics_json + updated_at，
        供前端实时展示调查轨迹指标。
        """
        db = await self._get_conn()
        await db.execute(
            "UPDATE sessions SET metrics_json=?, updated_at=? WHERE session_id=?",
            (json.dumps(metrics, ensure_ascii=False), _now(), session_id),
        )
        await db.commit()

    async def purge_old(self, retention_days: int) -> int:
        """Delete terminal-state sessions older than retention_days."""
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        db = await self._get_conn()
        # Delete associated events and PR proposals first (FK cascade may not cover pending_pr)
        await db.execute(
            "DELETE FROM pending_pr WHERE session_id IN "
            "(SELECT session_id FROM sessions WHERE status IN ('completed','failed','cancelled') AND updated_at < ?)",
            (cutoff,),
        )
        await db.execute(
            "DELETE FROM session_events WHERE session_id IN "
            "(SELECT session_id FROM sessions WHERE status IN ('completed','failed','cancelled') AND updated_at < ?)",
            (cutoff,),
        )
        cursor = await db.execute(
            "DELETE FROM sessions WHERE status IN ('completed','failed','cancelled') AND updated_at < ?",
            (cutoff,),
        )
        await db.commit()
        return cursor.rowcount

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


class SessionManager:
    def __init__(self, db_path: str | None = None) -> None:
        self._store: SqliteStore | MemoryStore
        if db_path and db_path != ":memory:":
            self._store = SqliteStore(db_path)
        else:
            self._store = MemoryStore()
        self._locks: dict[str, asyncio.Lock] = {}

    async def create(self, issue_url: str) -> Session:
        session = await self._store.create(issue_url)
        session.lock = self._locks.setdefault(session.session_id, asyncio.Lock())
        return session

    async def get(self, session_id: str) -> Session | None:
        session = await self._store.get(session_id)
        if session is not None:
            session.lock = self._locks.setdefault(session_id, asyncio.Lock())
            session.pending_pr = await self._store.get_pr_proposal(session_id)
        return session

    async def save(self, session: Session) -> None:
        await self._store.save(session)
        if session.pending_pr is not None:
            await self._store.save_pr_proposal(session.session_id, session.pending_pr)

    async def append_event(self, session_id: str, event: dict) -> dict:
        return await self._store.append_event(session_id, event)

    async def list_events(self, session_id: str) -> list[dict]:
        return await self._store.list_events(session_id)

    async def request_cancel(self, session_id: str) -> bool:
        return await self._store.request_cancel(session_id)

    async def is_cancel_requested(self, session_id: str) -> bool:
        return await self._store.is_cancel_requested(session_id)

    async def recover_stale(self, cutoff: str) -> int:
        return await self._store.recover_stale(cutoff)

    async def save_pr_proposal(self, session_id: str, proposal: dict) -> None:
        await self._store.save_pr_proposal(session_id, proposal)

    async def get_pr_proposal(self, session_id: str) -> dict | None:
        return await self._store.get_pr_proposal(session_id)

    async def delete_pr_proposal(self, session_id: str) -> None:
        await self._store.delete_pr_proposal(session_id)

    async def list(self, *, archived: bool = False, query: str = "", limit: int = 50) -> list[Session]:
        return await self._store.list(
            archived=archived,
            query=query,
            limit=max(1, min(limit, 100)),
        )

    async def delete(self, session_id: str) -> bool:
        deleted = await self._store.delete(session_id)
        if deleted:
            self._locks.pop(session_id, None)
        return deleted

    async def purge_old_sessions(self, retention_days: int) -> int:
        """Delete completed/failed/cancelled sessions older than retention_days.

        Returns the number of sessions purged.  Only supported on SqliteStore;
        MemoryStore is a no-op (returns 0).
        """
        if isinstance(self._store, SqliteStore):
            return await self._store.purge_old(retention_days)
        return 0

    async def update_metrics(self, session_id: str, metrics: dict) -> None:
        """轻量更新 metrics，不触发乐观锁版本递增。

        供实时调查流在工具调用时频繁刷新指标使用，避免与主 save() 竞争。
        """
        await self._store.update_metrics(session_id, metrics)

    async def close(self) -> None:
        if isinstance(self._store, SqliteStore):
            await self._store.close()


def _row_to_session(row: aiosqlite.Row) -> Session:
    """Deserialize a SQLite row into a Session dataclass instance."""
    now = _now()
    raw_status = row["status"] or "queued"
    status = (
        cast(SessionStatus, raw_status)
        if raw_status in {"queued", "running", "completed", "failed", "cancelled"}
        else "queued"
    )
    s = Session(
        session_id=row["session_id"],
        issue_url=row["issue_url"],
        display_title=row["display_title"],
        status=status,
        phase=row["phase"] or "queued",
        version=row["version"] or 0,
        cancel_requested=bool(row["cancel_requested"]),
        error_message=row["error_message"],
        archived_at=row["archived_at"],
        created_at=row["created_at"] or now,
        updated_at=row["updated_at"] or now,
    )
    if row["metrics_json"]:
        with suppress(Exception):
            s.metrics = json.loads(row["metrics_json"])
    if row["issue_json"]:
        with suppress(Exception):
            s.issue = IssueData.model_validate_json(row["issue_json"])
    if row["tree_json"]:
        with suppress(Exception):
            s.tree = json.loads(row["tree_json"])
    if row["messages_json"]:
        with suppress(Exception):
            s.messages = json.loads(row["messages_json"])
    if row["file_cache_json"]:
        with suppress(Exception):
            s.file_cache = json.loads(row["file_cache_json"])
    if row["files_read_json"]:
        with suppress(Exception):
            s.files_read = json.loads(row["files_read_json"])
    if row["report_json"]:
        with suppress(Exception):
            s.report = AnalysisReport.model_validate_json(row["report_json"])
    return s


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _session_search_text(session: Session) -> str:
    issue_title = session.issue.title if session.issue else ""
    repository = f"{session.issue.owner}/{session.issue.repo}" if session.issue else ""
    return " ".join((session.issue_url, session.display_title or "", issue_title, repository)).casefold()
