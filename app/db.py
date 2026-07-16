"""Async SQLite database layer for session persistence."""

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
    await conn.commit()
    return conn


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
