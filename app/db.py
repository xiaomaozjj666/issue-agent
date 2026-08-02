"""Async SQLite database layer for session persistence.

Schema is auto-created on first connection.  WAL journal mode is enabled for
concurrent read performance.  Migration helpers add columns introduced by
newer releases to databases created by older version.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Guard for one-shot migrations that should run exactly once per process lifetime,
# not per connection in the pool.  _migrate_report_enrichment does a full-table
# scan over every session with a report; running it pool_size times on cold start
# is wasteful and adds startup latency.
_enrichment_migration_done = False
_enrichment_migration_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    issue_url    TEXT NOT NULL,
    issue_json   TEXT,
    tree_json    TEXT DEFAULT '[]',
    messages_json TEXT DEFAULT '[]',
    file_cache_json TEXT DEFAULT '{}',
    files_read_json TEXT DEFAULT '[]',
    report_json  TEXT,
    display_title TEXT,
    status       TEXT NOT NULL DEFAULT 'queued',
    phase        TEXT NOT NULL DEFAULT 'queued',
    version      INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    archived_at  TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    event_type   TEXT NOT NULL,
    data_json    TEXT,
    message      TEXT NOT NULL DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_session_events_session_id
    ON session_events(session_id, id);

CREATE INDEX IF NOT EXISTS idx_session_events_created_at
    ON session_events(created_at);

CREATE TABLE IF NOT EXISTS pending_pr (
    session_id   TEXT PRIMARY KEY REFERENCES sessions(session_id),
    branch       TEXT NOT NULL,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    changes_json TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT DEFAULT (datetime('now'))
);
"""


async def get_db(path: str) -> aiosqlite.Connection:
    """Open (or create) the SQLite database and ensure schema + migrations are applied."""
    if path == ":memory:":
        conn = await aiosqlite.connect(":memory:")
    else:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(p))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript(SCHEMA)
    await _migrate_sessions(conn)
    await _migrate_report_enrichment_once(conn)
    await _ensure_performance_indexes(conn)
    await conn.commit()
    return conn


async def _ensure_performance_indexes(conn: aiosqlite.Connection) -> None:
    """Create indexes on migration-added columns (safe to call repeatedly)."""
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status_updated ON sessions(status, updated_at)")


async def _migrate_sessions(conn: aiosqlite.Connection) -> None:
    """Add session-history columns to databases created by older releases."""
    rows = await (await conn.execute("PRAGMA table_info(sessions)")).fetchall()
    existing = {row["name"] for row in rows}
    additions = {
        "display_title": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'queued'",
        "phase": "TEXT NOT NULL DEFAULT 'queued'",
        "version": "INTEGER NOT NULL DEFAULT 0",
        "metrics_json": "TEXT NOT NULL DEFAULT '{}'",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        "error_message": "TEXT",
        "archived_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in existing:
            await conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
    await conn.execute("UPDATE sessions SET status = 'completed' WHERE report_json IS NOT NULL AND status = 'queued'")


async def _migrate_report_enrichment(conn: aiosqlite.Connection) -> None:
    """Backfill enriched report fields for pre-senior-investigator reports.

    Idempotent: only fills missing *optional* fields (impact, hypotheses,
    evidence strength/kind, confidence_rationale, fix_rationale). Never
    overwrites data already produced by the LLM. Safe to run on every startup.
    """
    try:
        from app.report_backfill import enrich_report
    except Exception:  # pragma: no cover - enrichment is best-effort
        return
    rows = await (await conn.execute(
        "SELECT session_id, report_json FROM sessions WHERE report_json IS NOT NULL"
    )).fetchall()
    for row in rows:
        sid, rj = row["session_id"], row["report_json"]
        if not rj:
            continue
        try:
            rep = json.loads(rj)
        except (ValueError, TypeError):
            continue
        if not isinstance(rep, dict):
            continue
        try:
            enriched = enrich_report(rep)
        except Exception:
            continue
        new_rj = json.dumps(enriched, ensure_ascii=False)
        if new_rj != rj:
            await conn.execute(
                "UPDATE sessions SET report_json = ? WHERE session_id = ?",
                (new_rj, sid),
            )


async def _migrate_report_enrichment_once(conn: aiosqlite.Connection) -> None:
    """Run _migrate_report_enrichment at most once per process lifetime.

    The full-table scan is expensive and was previously called once per pooled
    connection (pool size 5 => 5 redundant scans on cold start).  This wrapper
    gates execution behind a module-level flag so the backfill runs only on the
    first connection to open.
    """
    global _enrichment_migration_done
    if _enrichment_migration_done:
        return
    async with _enrichment_migration_lock:
        if _enrichment_migration_done:
            return
        await _migrate_report_enrichment(conn)
        _enrichment_migration_done = True


class ConnectionPool:
    """SQLite 连接池：WAL 模式下允许多个读连接并发，写仍由 SQLite 串行。

    解决单连接瓶颈：实时调查流（高频 append_event + update_metrics）与前端
    轮询（list/get）竞争同一 aiosqlite.Connection 时，所有操作排队串行执行。
    池化后读操作可真正并发，写操作受 SQLite 自身锁约束仍串行。

    池大小默认 5：兼顾并发吞吐与文件句柄开销。LifoQueue 让最近用过的连接
    被优先复用，提升热点连接的缓存命中率。
    """

    def __init__(self, path: str, *, size: int = 5) -> None:
        self._path = path
        self._size = size
        self._pool: asyncio.LifoQueue[aiosqlite.Connection] = asyncio.LifoQueue()
        self._created = 0
        self._creation_lock = asyncio.Lock()
        # 跟踪借出但未归还的连接，close() 时也能关闭它们
        self._in_use: set[aiosqlite.Connection] = set()

    async def acquire(self) -> aiosqlite.Connection:
        """获取一个连接：优先复用空闲连接，不足时按需新建（不超过 size 上限）。"""
        try:
            conn = self._pool.get_nowait()
        except asyncio.QueueEmpty:
            pass
        else:
            self._in_use.add(conn)
            return conn
        async with self._creation_lock:
            if self._created < self._size:
                self._created += 1
                try:
                    conn = await get_db(self._path)
                except Exception:
                    self._created -= 1
                    raise
                self._in_use.add(conn)
                return conn
        # 已达上限：等待其他协程归还连接，10s 超时避免永久阻塞
        try:
            conn = await asyncio.wait_for(self._pool.get(), timeout=10.0)
        except TimeoutError as exc:
            raise RuntimeError("Database connection pool exhausted — all connections in use") from exc
        self._in_use.add(conn)
        return conn

    async def release(self, conn: aiosqlite.Connection) -> None:
        """归还连接到池中。连接已关闭则直接丢弃并减少计数，避免复用坏连接。"""
        self._in_use.discard(conn)
        # aiosqlite 关闭后内部 _conn 变为 None，不可复用
        if getattr(conn, "_conn", None) is None:
            async with self._creation_lock:
                if self._created > 0:
                    self._created -= 1
            return
        await self._pool.put(conn)

    @asynccontextmanager
    async def connection(self):
        """上下文管理器：自动获取并归还连接，异常时也保证归还。"""
        conn = await self.acquire()
        try:
            yield conn
        finally:
            await self.release(conn)

    async def close(self) -> None:
        """关闭池中所有连接（空闲 + 借出），防止进程关闭时泄漏。"""
        # 关闭空闲连接
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            with suppress(Exception):
                await conn.close()
        # 关闭仍在使用中的连接（异常关闭场景下防止泄漏）
        for conn in list(self._in_use):
            with suppress(Exception):
                await conn.close()
        self._in_use.clear()
        self._created = 0
