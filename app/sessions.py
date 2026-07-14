"""Session management with optional SQLite persistence."""

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass, field

from app.models import AnalysisReport, IssueData

logger = logging.getLogger(__name__)


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
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)


class MemoryStore:
    """In-memory session store (dev / testing)."""

    def __init__(self, max_sessions: int = 100) -> None:
        self._sessions: dict[str, Session] = {}
        self._max = max_sessions

    async def create(self, issue_url: str) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(session_id=sid, issue_url=issue_url)
        self._sessions[sid] = session
        self._evict()
        return session

    async def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def save(self, session: Session) -> None:
        pass  # already in memory

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

    def _evict(self) -> None:
        while len(self._sessions) > self._max:
            oldest = next(iter(self._sessions))
            del self._sessions[oldest]


class SqliteStore:
    """SQLite-backed session store (production)."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._conn: object = None

    async def _get_conn(self):
        if self._conn is None:
            from app.db import get_db

            self._conn = await get_db(self._path)
        return self._conn

    async def create(self, issue_url: str) -> Session:
        sid = uuid.uuid4().hex[:12]
        db = await self._get_conn()
        await db.execute("INSERT INTO sessions (session_id, issue_url) VALUES (?, ?)", (sid, issue_url))
        await db.commit()
        return Session(session_id=sid, issue_url=issue_url)

    async def get(self, session_id: str) -> Session | None:
        db = await self._get_conn()
        row = await (await db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))).fetchone()
        if row is None:
            return None
        return _row_to_session(row)

    async def save(self, session: Session) -> None:
        db = await self._get_conn()
        await db.execute(
            """UPDATE sessions SET issue_json=?, tree_json=?, messages_json=?, file_cache_json=?,
               files_read_json=?, report_json=?, updated_at=datetime('now')
               WHERE session_id=?""",
            (
                session.issue.model_dump_json() if session.issue else None,
                json.dumps(session.tree, ensure_ascii=False),
                json.dumps(session.messages, ensure_ascii=False, default=str),
                json.dumps(session.file_cache, ensure_ascii=False),
                json.dumps(session.files_read, ensure_ascii=False),
                session.report.model_dump_json() if session.report else None,
                session.session_id,
            ),
        )
        await db.commit()

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

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


class SessionManager:
    def __init__(self, db_path: str | None = None) -> None:
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

    async def save_pr_proposal(self, session_id: str, proposal: dict) -> None:
        await self._store.save_pr_proposal(session_id, proposal)

    async def get_pr_proposal(self, session_id: str) -> dict | None:
        return await self._store.get_pr_proposal(session_id)

    async def delete_pr_proposal(self, session_id: str) -> None:
        await self._store.delete_pr_proposal(session_id)

    async def close(self) -> None:
        if hasattr(self._store, "close"):
            await self._store.close()


def _row_to_session(row) -> Session:
    s = Session(session_id=row["session_id"], issue_url=row["issue_url"])
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
