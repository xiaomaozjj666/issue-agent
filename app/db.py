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
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);

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
    await conn.commit()
    return conn
