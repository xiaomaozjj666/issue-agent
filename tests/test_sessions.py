from app.sessions import Session, SessionManager


def test_create_returns_session_with_id_and_url() -> None:
    manager = SessionManager()

    session = manager.create("https://github.com/acme/widget/issues/1")

    assert session.session_id
    assert len(session.session_id) == 12
    assert session.issue_url == "https://github.com/acme/widget/issues/1"
    assert session.issue is None
    assert session.tree == []
    assert session.messages == []
    assert session.file_cache == {}
    assert session.files_read == []
    assert session.report is None


def test_create_generates_unique_ids() -> None:
    manager = SessionManager()
    ids = set()

    for _ in range(50):
        session = manager.create("https://github.com/a/b/issues/1")
        ids.add(session.session_id)

    assert len(ids) == 50


def test_get_returns_session_by_id() -> None:
    manager = SessionManager()
    session = manager.create("https://github.com/a/b/issues/1")

    retrieved = manager.get(session.session_id)

    assert retrieved is session


def test_get_returns_none_for_unknown_id() -> None:
    manager = SessionManager()

    assert manager.get("nonexistent") is None


def test_eviction_removes_oldest_when_exceeding_max() -> None:
    manager = SessionManager(max_sessions=3)

    s1 = manager.create("https://github.com/a/b/issues/1")
    s2 = manager.create("https://github.com/a/b/issues/2")
    s3 = manager.create("https://github.com/a/b/issues/3")
    s4 = manager.create("https://github.com/a/b/issues/4")

    assert manager.get(s1.session_id) is None
    assert manager.get(s2.session_id) is s2
    assert manager.get(s3.session_id) is s3
    assert manager.get(s4.session_id) is s4


def test_eviction_is_fifo() -> None:
    manager = SessionManager(max_sessions=2)

    s1 = manager.create("https://github.com/a/b/issues/1")
    s2 = manager.create("https://github.com/a/b/issues/2")
    s3 = manager.create("https://github.com/a/b/issues/3")

    assert manager.get(s1.session_id) is None
    assert manager.get(s2.session_id) is s2
    assert manager.get(s3.session_id) is s3

    s4 = manager.create("https://github.com/a/b/issues/4")

    assert manager.get(s2.session_id) is None
    assert manager.get(s3.session_id) is s3
    assert manager.get(s4.session_id) is s4


def test_session_dataclass_defaults() -> None:
    session = Session(session_id="abc123", issue_url="https://github.com/a/b/issues/1")

    assert session.issue is None
    assert session.tree == []
    assert session.messages == []
    assert session.file_cache == {}
    assert session.files_read == []
    assert session.report is None


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
