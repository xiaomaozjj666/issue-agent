"""Async SQLite database layer for session persistence.

Schema is auto-created on first connection.  WAL journal mode is enabled for
concurrent read performance.  Migration helpers add columns introduced by
newer releases to databases created by older versions.
"""

import logging
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
