import pytest

from app.sessions import Session, SessionManager


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


def test_session_dataclass_defaults() -> None:
    session = Session(session_id="abc123", issue_url="https://github.com/a/b/issues/1")
    assert session.issue is None
    assert session.tree == []
    assert session.messages == []
    assert session.file_cache == {}
    assert session.files_read == []
    assert session.report is None
    assert session.pending_pr is None


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
