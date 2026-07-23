"""Async SQLite database layer for session persistence.

Schema is auto-created on first connection.  WAL journal mode is enabled for
concurrent read performance.  Migration helpers add columns introduced by
newer releases to databases created by older version.
"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

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

    async def acquire(self) -> aiosqlite.Connection:
        """获取一个连接：优先复用空闲连接，不足时按需新建（不超过 size 上限）。"""
        try:
            return self._pool.get_nowait()
        except asyncio.QueueEmpty:
            pass
        async with self._creation_lock:
            if self._created < self._size:
                self._created += 1
                try:
                    return await get_db(self._path)
                except Exception:
                    self._created -= 1
                    raise
        # 已达上限：等待其他协程归还连接
        return await self._pool.get()

    async def release(self, conn: aiosqlite.Connection) -> None:
        """归还连接到池中。池已关闭或连接已关闭则直接丢弃。"""
        await self._pool.put(conn)

    @asynccontextmanager
    async def connection(self):
        """上下文管理器：自动获取并归还连接，异常时也保证归还。"""
        conn = await self.acquire()
        try:
            yield conn
        finally:
            await self._pool.put(conn)

    async def close(self) -> None:
        """关闭池中所有空闲连接。正在使用的连接由调用方自行关闭。"""
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            with suppress(Exception):
                await conn.close()
        self._created = 0
