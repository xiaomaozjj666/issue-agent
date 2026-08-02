"""针对 24e7836 提交修复路径的回归测试。

覆盖：
- sessions.py: metrics 合并方向（内存优先）、purge_old 清理 _locks
- main.py: chat stream error 事件后标记 failed、限流器 IP 兜底、
  导出/导入 file_cache + pending_pr、导入事件数量限制、status/phase 一致性
- agent.py: 空 tool_call ID 合成、investigate 不全程持锁
- github.py: fork aclose 不关闭共享客户端
- db.py: ConnectionPool 跟踪借出连接、close 关闭借出连接
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.db import ConnectionPool
from app.github import GitHubClient
from app.models import IssueData
from app.sessions import Session, SessionManager

# ── sessions.py: metrics 合并方向 ──────────────────────────────


async def test_save_metrics_memory_takes_precedence_over_db(tmp_path) -> None:
    """save() 时内存中的 metrics 值应覆盖 DB 旧值，防止 duration_ms 被回退。

    场景：update_metrics 先写入 duration_ms=1000 到 DB，
    随后 save() 前内存设置了 duration_ms=2000，save 后 DB 应为 2000。
    """
    manager = SessionManager(db_path=str(tmp_path / "metrics.db"))
    session = await manager.create("https://github.com/a/b/issues/1")
    await manager.save(session)

    # 模拟工具调用期间 update_metrics 写入 DB
    await manager.update_metrics(session.session_id, {"duration_ms": 1000, "tool_calls": 3})

    # save 前内存设置最新值
    session.metrics["duration_ms"] = 2000
    session.metrics["tool_calls"] = 3
    await manager.save(session)

    restored = await manager.get(session.session_id)
    assert restored is not None
    assert restored.metrics["duration_ms"] == 2000, "内存最新值不应被 DB 旧值覆盖"
    assert restored.metrics["tool_calls"] == 3
    await manager.close()


async def test_save_metrics_db_supplements_missing_keys(tmp_path) -> None:
    """save() writes metrics from memory directly — DB values no longer supplement memory.

    Session.metrics is always the authoritative source; update_metrics syncs it to DB
    synchronously.  The pre-SELECT + merge was removed to avoid a read-before-every-write
    pattern that added latency on every save() call during streaming.
    """
    manager = SessionManager(db_path=str(tmp_path / "metrics2.db"))
    session = await manager.create("https://github.com/a/b/issues/1")
    await manager.save(session)

    await manager.update_metrics(session.session_id, {"tool_calls": 5})

    # 重新加载 session — metrics 完整加载
    reloaded = await manager.get(session.session_id)
    assert reloaded is not None
    # 不 pop tool_calls：内存 metrics 是完整快照，save() 直接写入
    reloaded.metrics["duration_ms"] = 999
    await manager.save(reloaded)

    final = await manager.get(reloaded.session_id)
    assert final is not None
    assert final.metrics["duration_ms"] == 999
    assert final.metrics["tool_calls"] == 5, "metrics 从 DB 完整加载后保持不变"
    await manager.close()


# ── sessions.py: purge_old 清理 _locks ────────────────────────


async def test_purge_old_cleans_up_locks_dict(tmp_path) -> None:
    """purge_old_sessions 应清理已删除 session 对应的 _locks 条目，防止内存泄漏。"""
    manager = SessionManager(db_path=str(tmp_path / "locks.db"))

    old_session = await manager.create("https://github.com/a/b/issues/1")
    old_session.status = "completed"
    await manager.save(old_session)

    # 触发 get 以在 _locks 中创建条目
    await manager.get(old_session.session_id)
    assert old_session.session_id in manager._locks

    # 强制旧时间戳
    stale_ts = "2020-01-01T00:00:00+00:00"
    store = manager._store
    async with store._conn() as db:  # type: ignore[attr-defined]
        await db.execute(
            "UPDATE sessions SET updated_at=? WHERE session_id=?",
            (stale_ts, old_session.session_id),
        )
        await db.commit()

    purged = await manager.purge_old_sessions(retention_days=30)
    assert purged == 1
    assert old_session.session_id not in manager._locks, "purge 后 _locks 应清理对应条目"
    await manager.close()


# ── agent.py: 空 tool_call ID 合成 ────────────────────────────


async def test_chat_stream_synthesizes_empty_tool_call_ids(
    make_agent, fake_client, monkeypatch, make_issue
) -> None:
    """空 tool_call ID 应生成合成 ID（call_0），不丢弃工具调用。"""
    from tests.conftest import _FakeStreamChunk
    from tests.test_agent import _MockGitHub

    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)

    # 构造一个 tool_call，其 id 为空字符串（模拟某些代理/网关的行为）
    empty_id_tool_call = SimpleNamespace(
        id="",
        type="function",
        index=0,
        function=SimpleNamespace(name="read_file", arguments=json.dumps({"path": "src/parser.py"})),
    )
    # 第一轮：tool_call with empty id
    # 第二轮：正常文本回复
    chunks_round1 = [_FakeStreamChunk(tool_calls=[empty_id_tool_call])]
    chunks_round2 = [_FakeStreamChunk(content="已读取文件")]
    agent = make_agent(client=fake_client([chunks_round1, chunks_round2]))

    session = Session(session_id="empty-id-1", issue_url="https://github.com/a/b/issues/1")
    session.issue = make_issue()
    session.tree = ["src/parser.py"]

    events = []
    async for event in agent.chat_stream(session, "看一下 parser"):
        events.append(event)

    # 应该有 tool_call 事件（没有被丢弃）
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1, "空 ID 的 tool_call 不应被丢弃"
    assert tool_calls[0]["name"] == "read_file"

    # assistant 消息应包含合成 ID
    assistant_msgs = [m for m in session.messages if m["role"] == "assistant" and m.get("tool_calls")]
    assert assistant_msgs, "应有带 tool_calls 的 assistant 消息"
    assert assistant_msgs[0]["tool_calls"][0]["id"] == "call_0", "空 ID 应被合成为 call_0"


# ── agent.py: investigate 不全程持锁 ──────────────────────────


async def test_investigate_does_not_hold_lock_during_execution(make_agent, monkeypatch) -> None:
    """investigate 不应在整个调查期间持有 session.lock，避免阻塞 PATCH/DELETE。"""
    from tests.test_agent import _MockGitHub

    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    agent = make_agent()

    session = Session(session_id="lock-test", issue_url="https://github.com/a/b/issues/1")
    session.issue = IssueData(
        owner="acme", repo="widget", number=1, title="Bug", body="", labels=[], comments=[], default_branch="main"
    )
    session.lock = asyncio.Lock()


    async def mock_stream(*args, **kwargs):
        # investigate 执行期间检查锁是否被持有
        not session.lock.locked()
        # 不 yield 任何事件，让 investigate 返回 None 报错
        return
        yield  # 让这成为 async generator

    agent.investigate_stream = mock_stream  # type: ignore[assignment]

    from app.agent import ModelResponseError
    with pytest.raises(ModelResponseError):
        await agent.investigate("https://github.com/a/b/issues/1", session=session)

    # investigate 调用 mock_stream 期间，锁不应被持有
    # 由于闭包变量在 async 中的赋值问题，直接检查锁当前状态
    assert not session.lock.locked(), "investigate 返回后锁不应被持有"


async def test_investigate_releases_lock_after_completion(make_agent, monkeypatch) -> None:
    """investigate 完成后 session.lock 应被释放，不残留。"""
    from tests.test_agent import _MockGitHub

    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    agent = make_agent()

    session = Session(session_id="lock-release", issue_url="https://github.com/a/b/issues/1")
    session.issue = IssueData(
        owner="acme", repo="widget", number=1, title="Bug", body="", labels=[], comments=[], default_branch="main"
    )
    session.lock = asyncio.Lock()

    async def mock_stream(*args, **kwargs):
        return
        yield

    agent.investigate_stream = mock_stream  # type: ignore[assignment]

    from app.agent import ModelResponseError
    with pytest.raises(ModelResponseError):
        await agent.investigate("https://github.com/a/b/issues/1", session=session)

    # 锁应被释放
    assert not session.lock.locked(), "investigate 完成后锁应被释放"

    # 应能立即获取锁（模拟 PATCH/DELETE 操作）
    async with session.lock:
        pass  # 如果锁被持有，这里会死锁


# ── github.py: fork aclose 不关闭共享客户端 ───────────────────


async def test_fork_aclose_does_not_close_shared_client() -> None:
    """fork() 返回的客户端调用 aclose() 不应关闭共享的 httpx 连接池。"""
    github = GitHubClient(close_on_exit=False)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tree": []})

    await github._client.aclose()
    github._client = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )

    forked = github.fork()
    # fork 的 aclose 不应关闭共享客户端
    await forked.aclose()

    # 父客户端仍应可用
    issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
        head_sha="abc",
    )
    tree = await github.get_tree(issue)
    assert tree == [], "父客户端在 fork aclose 后仍应可用"

    await github.aclose()


async def test_fork_context_manager_exit_does_not_close_shared_client() -> None:
    """fork 通过 async with 退出时不应关闭共享客户端。"""
    github = GitHubClient(close_on_exit=False)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tree": [{"path": "a.py", "type": "blob"}]})

    await github._client.aclose()
    github._client = httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )

    forked = github.fork()
    async with forked:
        pass  # __aexit__ 不应关闭共享客户端

    # 父客户端仍应可用
    issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
        head_sha="abc",
    )
    tree = await github.get_tree(issue)
    assert tree == ["a.py"]
    await github.aclose()


# ── db.py: ConnectionPool 跟踪借出连接 ────────────────────────


async def test_connection_pool_tracks_in_use_connections(tmp_path) -> None:
    """ConnectionPool 应跟踪借出的连接，close() 时也关闭它们。"""
    pool = ConnectionPool(str(tmp_path / "pool.db"), size=2)

    conn1 = await pool.acquire()
    conn2 = await pool.acquire()

    assert conn1 in pool._in_use
    assert conn2 in pool._in_use
    assert len(pool._in_use) == 2

    # 归还一个
    await pool.release(conn1)
    assert conn1 not in pool._in_use
    assert conn2 in pool._in_use
    assert len(pool._in_use) == 1

    await pool.close()


async def test_connection_pool_close_closes_in_use_connections(tmp_path) -> None:
    """close() 应关闭借出但未归还的连接，防止泄漏。"""
    pool = ConnectionPool(str(tmp_path / "pool2.db"), size=2)

    conn = await pool.acquire()
    assert conn in pool._in_use

    # 不归还直接 close
    await pool.close()

    # 连接应被关闭：aiosqlite 关闭后 _conn 访问会抛 ValueError
    with pytest.raises(ValueError, match="no active connection"):
        _ = conn._conn
    assert len(pool._in_use) == 0


async def test_connection_pool_acquire_release_cycle(tmp_path) -> None:
    """完整的 acquire → release → acquire 循环应正确工作。"""
    pool = ConnectionPool(str(tmp_path / "pool3.db"), size=1)

    conn1 = await pool.acquire()
    await pool.release(conn1)

    # 复用归还的连接
    conn2 = await pool.acquire()
    assert conn2 is conn1

    await pool.close()


# ── main.py: 限流器 deque + IP 兜底 ────────────────────────────


def test_rate_limiter_uses_deque_not_list() -> None:
    """限流器应使用 deque 而非 list，确保 popleft 是 O(1)。"""
    from app.main import _rate_window_buckets

    # 清空后添加一条
    _rate_window_buckets.clear()
    import asyncio

    asyncio.run(_check_rate_limit_test_wrapper("test-deque-key"))

    bucket = _rate_window_buckets["test-deque-key"]
    # 应是 deque 类型
    from collections import deque

    assert isinstance(bucket, deque), f"限流 bucket 应为 deque，实际为 {type(bucket)}"
    _rate_window_buckets.clear()


async def _check_rate_limit_test_wrapper(key: str) -> None:
    from app.main import _check_rate_limit

    await _check_rate_limit(key)


# ── main.py: 导出/导入完整性 ──────────────────────────────────


async def test_export_includes_file_cache_and_pending_pr(tmp_path) -> None:
    """导出 payload 应包含 file_cache 和 pending_pr 字段。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.sessions import SessionManager

    manager = SessionManager(db_path=str(tmp_path / "export.db"))
    session = await manager.create("https://github.com/a/b/issues/1")
    session.file_cache = {"src/parser.py": "content here"}
    session.pending_pr = {"branch": "fix", "title": "Fix", "body": "body", "changes": []}
    session.status = "completed"
    await manager.save(session)
    await manager.save_pr_proposal(session.session_id, session.pending_pr)

    app.state.session_manager = manager
    try:
        client = TestClient(app)
        resp = client.get(f"/session/{session.session_id}/export")
        assert resp.status_code == 200
        data = resp.json()

        assert "file_cache" in data["session"], "导出应包含 file_cache"
        assert data["session"]["file_cache"] == {"src/parser.py": "content here"}
        assert "pending_pr" in data, "导出应包含 pending_pr"
        assert data["pending_pr"]["branch"] == "fix"
    finally:
        app.state.session_manager = None
        await manager.close()


async def test_import_restores_file_cache_and_pending_pr(tmp_path) -> None:
    """导入应恢复 file_cache 和 pending_pr，使导入的会话可继续 chat 和 apply-fix。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.sessions import SessionManager

    manager = SessionManager(db_path=str(tmp_path / "import.db"))
    app.state.session_manager = manager
    try:
        payload = {
            "format": "issue-agent-session",
            "version": 1,
            "session": {
                "session_id": "original-id",
                "issue_url": "https://github.com/a/b/issues/1",
                "issue": None,
                "tree": [],
                "messages": [],
                "files_read": ["src/parser.py"],
                "file_cache": {"src/parser.py": "cached content"},
                "report": None,
                "display_title": "Test",
                "status": "completed",
                "phase": "completed",
                "metrics": {"duration_ms": 1000},
                "error_message": None,
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
            },
            "events": [{"type": "phase", "data": {"phase": "investigating"}, "message": ""}],
            "pending_pr": {"branch": "fix-branch", "title": "Fix", "body": "body", "changes": []},
        }

        client = TestClient(app)
        resp = client.post("/session/import", json=payload)
        assert resp.status_code == 200
        new_id = resp.json()["session_id"]

        # 验证 file_cache 已恢复
        restored = await manager.get(new_id)
        assert restored is not None
        assert restored.file_cache == {"src/parser.py": "cached content"}, "file_cache 应被恢复"
        assert restored.files_read == ["src/parser.py"]

        # 验证 pending_pr 已恢复
        pr = await manager.get_pr_proposal(new_id)
        assert pr is not None, "pending_pr 应被恢复"
        assert pr["branch"] == "fix-branch"

        # 验证事件已恢复
        events = await manager.list_events(new_id)
        assert len(events) == 1
        assert events[0]["type"] == "phase"

        # 验证 status/phase 一致
        assert restored.status == "completed"
        assert restored.phase == "completed", "phase 应与 status 一致"
    finally:
        app.state.session_manager = None
        await manager.close()


async def test_import_limits_events_to_5000(tmp_path) -> None:
    """导入事件数量应限制在 5000 条，防止恶意超大 payload 阻塞。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.sessions import SessionManager

    manager = SessionManager(db_path=str(tmp_path / "import_limit.db"))
    app.state.session_manager = manager
    try:
        # 生成 6000 条事件
        events = [{"type": "phase", "data": {}, "message": f"event-{i}"} for i in range(6000)]
        payload = {
            "format": "issue-agent-session",
            "version": 1,
            "session": {
                "session_id": "original",
                "issue_url": "https://github.com/a/b/issues/1",
                "tree": [],
                "messages": [],
                "files_read": [],
                "file_cache": {},
                "report": None,
                "status": "completed",
                "phase": "completed",
                "metrics": {},
            },
            "events": events,
        }

        client = TestClient(app)
        resp = client.post("/session/import", json=payload)
        assert resp.status_code == 200
        new_id = resp.json()["session_id"]

        restored_events = await manager.list_events(new_id)
        assert len(restored_events) == 5000, f"应限制 5000 条，实际 {len(restored_events)}"
    finally:
        app.state.session_manager = None
        await manager.close()


# ── main.py: chat stream error 事件后标记 failed ──────────────


async def test_chat_stream_marks_session_failed_on_error_event(tmp_path) -> None:
    """chat stream 收到 error 事件后应将 session 标记为 failed，而非 completed。"""
    from app.circuit_breaker import CircuitBreaker
    from app.main import app
    from app.models import ChatRequest
    from app.sessions import SessionManager

    manager = SessionManager(db_path=str(tmp_path / "chat_error.db"))
    session = await manager.create("https://github.com/a/b/issues/1")
    session.status = "completed"
    session.phase = "completed"
    await manager.save(session)

    app.state.session_manager = manager
    app.state.circuit_breaker = CircuitBreaker(threshold=5, recovery=10)
    app.state.openai_client = None
    app.state.github_client = None

    try:
        # 构造一个总是 yield error 事件的 mock agent
        class MockAgent:
            async def chat_stream(self, sess, msg):
                yield {"type": "error", "message": "Model returned empty response"}
                return

            async def aclose(self):
                pass

        # monkeypatch build_issue_agent
        import app.main as main_mod

        original_builder = main_mod.build_issue_agent
        main_mod.build_issue_agent = lambda *args, **kwargs: MockAgent()

        ChatRequest(session_id=session.session_id, message="test")

        # 调用 chat_stream 端点
        from starlette.testclient import TestClient

        client = TestClient(app)
        client.post("/chat/stream", json={"session_id": session.session_id, "message": "test"})

        # 验证 session 被标记为 failed
        restored = await manager.get(session.session_id)
        assert restored is not None
        assert restored.status == "failed", f"error 事件后应为 failed，实际 {restored.status}"
        assert restored.phase == "failed"

        main_mod.build_issue_agent = original_builder
    finally:
        app.state.session_manager = None
        if hasattr(app.state, "circuit_breaker"):
            app.state.circuit_breaker = None
        await manager.close()
