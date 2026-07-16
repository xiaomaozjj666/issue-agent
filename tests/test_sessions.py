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
    with sqlite3.connect(path) as connection:
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
