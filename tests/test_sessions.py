import sqlite3

import pytest

from app.db import get_db
from app.sessions import Session, SessionConflictError, SessionManager


@pytest.fixture
async def manager():
    return SessionManager()


async def test_create_returns_session_with_id_and_url(manager) -> None:
    session = await manager.create("https://github.com/acme/widget/issues/1")
    assert session.session_id
    assert len(session.session_id) == 12
    assert session.issue_url == "https://github.com/acme/widget/issues/1"
    assert session.issue is None
    assert session.tree == []
    assert session.messages == []
    assert session.file_cache == {}
    assert session.files_read == []
    assert session.report is None
    assert session.pending_pr is None
    assert session.phase == "queued"
    assert session.version == 0
    assert session.metrics == {}


async def test_create_generates_unique_ids(manager) -> None:
    ids = set()
    for _ in range(50):
        session = await manager.create("https://github.com/a/b/issues/1")
        ids.add(session.session_id)
    assert len(ids) == 50


async def test_get_returns_session_by_id(manager) -> None:
    session = await manager.create("https://github.com/a/b/issues/1")
    retrieved = await manager.get(session.session_id)
    assert retrieved is session


async def test_get_returns_none_for_unknown_id(manager) -> None:
    assert await manager.get("nonexistent") is None


async def test_sqlite_persists_proposal_and_reuses_session_lock(tmp_path) -> None:
    db_path = str(tmp_path / "sessions.db")
    manager = SessionManager(db_path=db_path)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.pending_pr = {
        "branch": "fix/issue-1",
        "title": "Fix",
        "body": "Body",
        "changes": [],
    }
    await manager.save(session)

    first = await manager.get(session.session_id)
    second = await manager.get(session.session_id)

    assert first is not None and second is not None
    assert first.lock is second.lock
    assert first.pending_pr == session.pending_pr
    await manager.close()


async def test_sqlite_persists_event_history_and_metrics(tmp_path) -> None:
    manager = SessionManager(db_path=str(tmp_path / "sessions.db"))
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.phase = "exploring"
    session.metrics = {"model_calls": 2, "tool_calls": 1}
    await manager.save(session)
    await manager.append_event(
        session.session_id,
        {"type": "phase", "data": {"phase": "exploring", "label": "Exploring"}, "message": ""},
    )

    restored = await manager.get(session.session_id)
    events = await manager.list_events(session.session_id)

    assert restored is not None
    assert restored.phase == "exploring"
    assert restored.metrics == {"model_calls": 2, "tool_calls": 1}
    assert events[0]["type"] == "phase"
    assert events[0]["data"]["label"] == "Exploring"
    await manager.close()


async def test_sqlite_detects_concurrent_session_updates(tmp_path) -> None:
    manager = SessionManager(db_path=str(tmp_path / "sessions.db"))
    created = await manager.create("https://github.com/acme/widget/issues/1")
    first = await manager.get(created.session_id)
    second = await manager.get(created.session_id)
    assert first is not None and second is not None

    first.display_title = "First writer"
    await manager.save(first)
    second.display_title = "Stale writer"
    with pytest.raises(SessionConflictError):
        await manager.save(second)

    restored = await manager.get(created.session_id)
    assert restored is not None
    assert restored.display_title == "First writer"
    await manager.close()


async def test_cancel_request_and_stale_recovery(manager) -> None:
    running = await manager.create("https://github.com/acme/widget/issues/1")
    running.status = "running"
    running.updated_at = "2020-01-01T00:00:00+00:00"

    assert await manager.request_cancel(running.session_id) is True
    assert await manager.is_cancel_requested(running.session_id) is True

    running.cancel_requested = False
    running.updated_at = "2020-01-01T00:00:00+00:00"
    assert await manager.recover_stale("2021-01-01T00:00:00+00:00") == 1
    assert running.status == "failed"
    assert running.phase == "interrupted"


async def test_session_history_filters_searches_and_deletes(manager) -> None:
    active = await manager.create("https://github.com/acme/widget/issues/1")
    active.display_title = "Parser failure"
    active.status = "completed"
    await manager.save(active)
    archived = await manager.create("https://github.com/acme/api/issues/2")
    archived.display_title = "Authentication timeout"
    archived.archived_at = "2026-01-01T00:00:00+00:00"
    await manager.save(archived)

    assert [item.session_id for item in await manager.list()] == [active.session_id]
    assert [item.session_id for item in await manager.list(archived=True)] == [archived.session_id]
    assert [item.session_id for item in await manager.list(query="parser")] == [active.session_id]
    assert await manager.delete(active.session_id) is True
    assert await manager.get(active.session_id) is None


async def test_sqlite_migrates_legacy_session_schema(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                issue_url TEXT NOT NULL,
                issue_json TEXT,
                tree_json TEXT DEFAULT '[]',
                messages_json TEXT DEFAULT '[]',
                file_cache_json TEXT DEFAULT '{}',
                files_read_json TEXT DEFAULT '[]',
                report_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )"""
        )
        connection.execute(
            """CREATE TABLE pending_pr (
                session_id TEXT PRIMARY KEY,
                branch TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                changes_json TEXT NOT NULL DEFAULT '[]'
            )"""
        )
        connection.commit()
    finally:
        connection.close()

    db = await get_db(str(path))
    columns = {row["name"] for row in await (await db.execute("PRAGMA table_info(sessions)")).fetchall()}
    await db.close()

    assert {
        "display_title",
        "status",
        "phase",
        "version",
        "metrics_json",
        "cancel_requested",
        "error_message",
        "archived_at",
    } <= columns


def test_session_dataclass_defaults() -> None:
    session = Session(session_id="abc123", issue_url="https://github.com/a/b/issues/1")
    assert session.issue is None
    assert session.tree == []
    assert session.messages == []
    assert session.file_cache == {}
    assert session.files_read == []
    assert session.report is None
    assert session.pending_pr is None
    assert session.phase == "queued"
    assert session.metrics == {}


def test_session_dataclass_mutable_defaults_are_independent() -> None:
    s1 = Session(session_id="a", issue_url="u1")
    s2 = Session(session_id="b", issue_url="u2")
    s1.tree.append("src/a.py")
    s1.messages.append({"role": "user", "content": "hi"})
    s1.file_cache["x"] = "y"
    s1.files_read.append("src/a.py")
    assert s2.tree == []
    assert s2.messages == []
    assert s2.file_cache == {}
    assert s2.files_read == []


async def test_purge_old_deletes_terminal_sessions_beyond_retention(tmp_path) -> None:
    """Completed/failed/cancelled sessions older than retention_days are purged."""
    manager = SessionManager(db_path=str(tmp_path / "sessions.db"))

    old_completed = await manager.create("https://github.com/a/b/issues/1")
    old_completed.status = "completed"
    await manager.save(old_completed)

    old_failed = await manager.create("https://github.com/a/b/issues/2")
    old_failed.status = "failed"
    await manager.save(old_failed)

    old_cancelled = await manager.create("https://github.com/a/b/issues/3")
    old_cancelled.status = "cancelled"
    await manager.save(old_cancelled)

    fresh_running = await manager.create("https://github.com/a/b/issues/4")
    fresh_running.status = "running"
    await manager.save(fresh_running)

    # save() refreshes updated_at to now; force old timestamps directly in the DB
    # so the purge query sees them as beyond the retention window.
    stale_ts = "2020-01-01T00:00:00+00:00"
    store = manager._store
    if hasattr(store, "_get_conn"):
        db = await store._get_conn()
        for sid in (
            old_completed.session_id,
            old_failed.session_id,
            old_cancelled.session_id,
            fresh_running.session_id,
        ):
            await db.execute("UPDATE sessions SET updated_at=? WHERE session_id=?", (stale_ts, sid))
        await db.commit()

    purged = await manager.purge_old_sessions(retention_days=30)
    assert purged == 3
    assert await manager.get(old_completed.session_id) is None
    assert await manager.get(old_failed.session_id) is None
    assert await manager.get(old_cancelled.session_id) is None
    assert await manager.get(fresh_running.session_id) is not None
    await manager.close()


async def test_purge_old_keeps_recent_terminal_sessions(tmp_path) -> None:
    """Sessions within the retention window are kept regardless of status."""
    manager = SessionManager(db_path=str(tmp_path / "sessions.db"))

    recent = await manager.create("https://github.com/a/b/issues/1")
    recent.status = "completed"
    await manager.save(recent)
    # save() already sets updated_at to now, so the session is within the window.

    purged = await manager.purge_old_sessions(retention_days=30)
    assert purged == 0
    assert await manager.get(recent.session_id) is not None
    await manager.close()


async def test_purge_old_is_noop_on_memory_store() -> None:
    """MemoryStore is a no-op for purge (returns 0)."""
    manager = SessionManager()
    session = await manager.create("https://github.com/a/b/issues/1")
    session.status = "completed"
    session.updated_at = "2020-01-01T00:00:00+00:00"
    await manager.save(session)

    assert await manager.purge_old_sessions(retention_days=1) == 0
    assert await manager.get(session.session_id) is not None


async def test_update_metrics_persists_without_version_bump(tmp_path) -> None:
    """Lightweight metrics update writes metrics_json without bumping session version."""
    manager = SessionManager(db_path=str(tmp_path / "sessions.db"))
    session = await manager.create("https://github.com/a/b/issues/1")
    await manager.save(session)

    version_before = session.version
    await manager.update_metrics(
        session.session_id,
        {"model_calls": 5, "tool_calls": 7, "files_read": 2, "duration_ms": 1234},
    )

    restored = await manager.get(session.session_id)
    assert restored is not None
    assert restored.metrics["model_calls"] == 5
    assert restored.metrics["tool_calls"] == 7
    assert restored.metrics["files_read"] == 2
    # Version must not change — update_metrics must not conflict with concurrent save().
    assert restored.version == version_before
    await manager.close()


async def test_update_metrics_works_on_memory_store() -> None:
    """MemoryStore.update_metrics writes back to the in-memory session object."""
    manager = SessionManager()
    session = await manager.create("https://github.com/a/b/issues/1")
    await manager.update_metrics(session.session_id, {"model_calls": 3, "tool_calls": 4})

    restored = await manager.get(session.session_id)
    assert restored is not None
    assert restored.metrics["model_calls"] == 3
    assert restored.metrics["tool_calls"] == 4
