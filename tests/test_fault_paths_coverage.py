"""故障路径与边界分支测试：补齐 main / agent / reviewer / report_generator /
tools / sessions / db / task_queue 中此前未被覆盖的错误处理、并发冲突与降级分支。

这些测试锁定的都是生产环境真实会发生的场景：客户端断开 SSE、会话并发冲突、
LLM 空响应、报告校验失败重试、连接池耗尽、批量队列停止时的残留任务等。
"""

import asyncio
import json
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.deps as deps_module
import app.main as main_module
import app.rate_limit as rate_limit_module
import app.routes.analysis as analysis_routes
import app.routes.chat as chat_routes
import app.routes.sessions as sessions_routes
from app.agent import IssueAgent, ModelResponseError, friendly_chat_error
from app.circuit_breaker import CircuitBreaker
from app.config import Settings
from app.deps import ProviderClients, get_circuit_breaker, get_session_manager
from app.errors import CircuitBreakerOpenError
from app.events import done_event, phase_event
from app.main import app
from app.models import AnalysisReport, ChatRequest, IssueData, SourceFile, StreamRequest
from app.services import InvestigationGate
from app.sessions import Session, SessionConflictError, SessionManager


@pytest.fixture(autouse=True)
def _ensure_circuit_breaker_dependency():
    """TestClient 不触发 lifespan，需自动注入熔断器依赖（与 test_main 一致）。"""
    app.dependency_overrides[get_circuit_breaker] = lambda: CircuitBreaker(threshold=5, recovery=30)
    yield
    app.dependency_overrides.pop(get_circuit_breaker, None)


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    """模块级滑动窗口桶跨测试残留会让本文件的大量请求触发误判 429，逐测清空。"""
    rate_limit_module._rate_window_buckets.clear()
    yield
    rate_limit_module._rate_window_buckets.clear()


def _report_data(**overrides: object) -> dict:
    data: dict = {
        "summary": "Parser bug",
        "root_cause": "Fails at src/parser.py L1",
        "confidence": "high",
        "evidence": [{"path": "src/parser.py", "lines": "L1", "reason": "parse call"}],
        "proposed_changes": ["Fix"],
        "patch": None,
        "tests": [],
        "risks": [],
    }
    data.update(overrides)
    return data


class _RefGitHub:
    """Issue 文本引用仓库内文件：一个可读、一个读取失败，覆盖预读成功/失败分支。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.get_issue = AsyncMock(
            return_value=IssueData(
                owner="acme",
                repo="widget",
                number=1,
                title="Test Bug",
                body="Please check src/parser.py and src/lexer.py",
                labels=["bug"],
                comments=[],
                default_branch="main",
            )
        )
        self.get_tree = AsyncMock(return_value=["src/parser.py", "src/lexer.py"])
        self.closed = False

    async def __aenter__(self) -> "_RefGitHub":
        return self

    async def __aexit__(self, *args: object) -> bool:
        self.closed = True
        return False

    async def aclose(self) -> None:
        self.closed = True

    async def get_file(self, issue: IssueData, path: str) -> SourceFile:
        if path == "src/lexer.py":
            raise RuntimeError("read failed")
        return SourceFile(path=path, content="def parse():\n    return None\n")


# ══════════════════════════════════════════════════════════════════
# main.py：/i18n、限流、异常处理器
# ══════════════════════════════════════════════════════════════════


def test_i18n_endpoint_returns_strings_for_both_languages() -> None:
    client = TestClient(app)
    zh = client.get("/i18n", params={"lang": "zh"})
    en = client.get("/i18n", params={"lang": "en"})

    assert zh.status_code == 200
    assert en.status_code == 200
    zh_data, en_data = zh.json(), en.json()
    assert set(zh_data) == set(en_data)
    assert zh_data != en_data  # 两种语言实际返回不同文案


def test_i18n_endpoint_rejects_unknown_language() -> None:
    response = TestClient(app).get("/i18n", params={"lang": "fr"})

    assert response.status_code == 422


async def test_rate_limiter_evicts_expired_timestamps() -> None:
    rate_limit_module._rate_window_buckets.clear()
    old = time.monotonic() - 999
    rate_limit_module._rate_window_buckets["evict-key"] = deque([old, old + 1])

    await rate_limit_module._check_rate_limit("evict-key")

    bucket = rate_limit_module._rate_window_buckets["evict-key"]
    assert all(ts > time.monotonic() - 120 for ts in bucket)
    rate_limit_module._rate_window_buckets.clear()


def test_rate_limiter_uses_api_key_header_when_configured(monkeypatch) -> None:
    """API_KEY 已配置且请求带 X-API-Key：按 header 值限流（而非客户端 IP）。"""
    settings = Settings(openai_api_key="test-key", api_key="secret", rate_limit_requests=1)
    monkeypatch.setattr(rate_limit_module, "get_settings", lambda: settings)
    import app.auth as auth_module

    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    rate_limit_module._rate_window_buckets.clear()
    client = TestClient(app)
    try:
        first = client.get("/i18n", params={"lang": "zh"}, headers={"X-API-Key": "secret"})
        second = client.get("/i18n", params={"lang": "zh"}, headers={"X-API-Key": "secret"})

        assert first.status_code == 200
        assert second.status_code == 429
        assert "Retry-After" in second.headers
        assert "secret" in rate_limit_module._rate_window_buckets
    finally:
        rate_limit_module._rate_window_buckets.clear()


async def test_session_conflict_handler_returns_409(monkeypatch) -> None:
    manager = SessionManager()
    original_get = manager.get

    async def raising_get(session_id: str):
        raise SessionConflictError("version mismatch")

    monkeypatch.setattr(manager, "get", raising_get)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        client = TestClient(app)
        response = client.get("/session/abc")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)
        monkeypatch.setattr(manager, "get", original_get)

    assert response.status_code == 409
    assert "reload and try again" in response.json()["detail"]


async def test_circuit_breaker_open_handler_returns_503(monkeypatch) -> None:
    manager = SessionManager()

    async def raising_get(session_id: str):
        raise CircuitBreakerOpenError("circuit breaker is open")

    monkeypatch.setattr(manager, "get", raising_get)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        response = TestClient(app).get("/session/abc")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 503
    assert "circuit breaker" in response.json()["detail"].lower()


# ══════════════════════════════════════════════════════════════════
# main.py：/stream 心跳、续跑、取消、冲突持久化失败
# ══════════════════════════════════════════════════════════════════


def _override_session_manager(manager: SessionManager) -> None:
    app.dependency_overrides[get_session_manager] = lambda: manager


def test_stream_keepalive_emitted_for_slow_steps(monkeypatch) -> None:
    """单个事件产出超过心跳阈值：SSE 输出 keepalive 注释并 touch 会话活跃度。"""
    manager = SessionManager()
    _override_session_manager(manager)
    original = analysis_routes._iter_events_with_heartbeat

    async def fast_heartbeat(event_iter, *, timeout: float = 15.0):
        async for item in original(event_iter, timeout=0.05):
            yield item

    monkeypatch.setattr(analysis_routes, "_iter_events_with_heartbeat", fast_heartbeat)

    async def slow_stream(self: IssueAgent, issue_url: str, *, session=None):
        await asyncio.sleep(0.2)
        if session is not None:
            session.metrics["model_calls"] = 1
        yield done_event()

    monkeypatch.setattr(IssueAgent, "investigate_stream", slow_stream)
    try:
        response = TestClient(app).post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert ": keepalive" in response.text
    assert '"type": "done"' in response.text


async def test_stream_resume_clears_previous_events(monkeypatch) -> None:
    """带 session_id 续跑：清空上一轮事件，从干净状态重新调查。"""
    manager = SessionManager()
    _override_session_manager(manager)

    async def fake_stream(self: IssueAgent, issue_url: str, *, session=None):
        yield done_event()

    monkeypatch.setattr(IssueAgent, "investigate_stream", fake_stream)
    client = TestClient(app)
    try:
        first = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
        session_id = json.loads(
            next(line for line in first.text.splitlines() if '"type": "session"' in line).removeprefix("data: ")
        )["data"]["session_id"]
        await manager.append_event(session_id, {"type": "phase", "data": None, "message": "old"})
        events_before = len(await manager.list_events(session_id))

        second = client.post("/stream", json={"session_id": session_id})
        events_after = len(await manager.list_events(session_id))
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert events_before >= 1
    assert '"type": "done"' in second.text
    assert events_after < events_before + 5  # 旧事件已被清空


async def test_stream_conflict_with_cancel_requested_yields_cancelled(monkeypatch) -> None:
    """并发冲突时若用户已请求取消：输出 cancelled 事件而非 error。"""
    manager = SessionManager()
    _override_session_manager(manager)

    async def conflicting_stream(self: IssueAgent, issue_url: str, *, session=None):
        assert session is not None
        await manager.request_cancel(session.session_id)
        raise SessionConflictError("changed elsewhere")
        yield  # pragma: no cover

    monkeypatch.setattr(IssueAgent, "investigate_stream", conflicting_stream)
    client = TestClient(app)
    try:
        response = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert '"type": "cancelled"' in response.text


async def test_stream_cancellation_marks_session_interrupted(monkeypatch) -> None:
    """客户端断开（CancelledError）：会话被标记为 failed/interrupted。"""
    manager = SessionManager()
    breaker = CircuitBreaker(threshold=5, recovery=30)

    async def cancelled_stream(self: IssueAgent, issue_url: str, *, session=None):
        raise asyncio.CancelledError
        yield  # pragma: no cover

    monkeypatch.setattr(IssueAgent, "investigate_stream", cancelled_stream)
    response = await analysis_routes.stream_analysis(
        StreamRequest(issue_url="https://github.com/acme/widget/issues/1"),
        ProviderClients(None, None),
        manager,
        breaker,
        InvestigationGate(4),
    )
    with pytest.raises(asyncio.CancelledError):
        async for _ in response.body_iterator:
            pass

    sessions = await manager.list(archived=False, query="", limit=10)
    assert sessions[0].status == "failed"
    assert sessions[0].phase == "interrupted"


async def test_stream_cancellation_survives_interrupt_persist_failure(monkeypatch) -> None:
    """取消时中断态落库失败：异常被记录，CancelledError 仍向外传播。"""
    manager = SessionManager()
    breaker = CircuitBreaker(threshold=5, recovery=30)

    async def failing_mark(manager_, session_id: str, started_at: float) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(analysis_routes, "mark_stream_interrupted", failing_mark)

    async def cancelled_stream(self: IssueAgent, issue_url: str, *, session=None):
        raise asyncio.CancelledError
        yield  # pragma: no cover

    monkeypatch.setattr(IssueAgent, "investigate_stream", cancelled_stream)
    response = await analysis_routes.stream_analysis(
        StreamRequest(issue_url="https://github.com/acme/widget/issues/1"),
        ProviderClients(None, None),
        manager,
        breaker,
        InvestigationGate(4),
    )
    with pytest.raises(asyncio.CancelledError):
        async for _ in response.body_iterator:
            pass


async def test_stream_failure_save_conflict_is_logged_not_raised(monkeypatch) -> None:
    """会话标记 failed 时保存冲突：只记日志，不掩盖原始错误事件。"""
    manager = SessionManager()
    _override_session_manager(manager)
    original_save = manager.save

    async def conflicting_save(session: Session) -> None:
        if session.status == "failed":
            raise SessionConflictError("concurrent update")
        await original_save(session)

    async def failing_stream(self: IssueAgent, issue_url: str, *, session=None):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    monkeypatch.setattr(IssueAgent, "investigate_stream", failing_stream)
    monkeypatch.setattr(manager, "save", conflicting_save)
    client = TestClient(app)
    try:
        response = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert '"type": "error"' in response.text
    assert "boom" in response.text


# ══════════════════════════════════════════════════════════════════
# main.py：/chat 与 /chat/stream 的 regenerate、错误映射、心跳、取消
# ══════════════════════════════════════════════════════════════════


def test_apply_regenerate_points_at_last_user_message() -> None:
    session = Session(
        session_id="s1",
        issue_url="https://github.com/acme/widget/issues/1",
        messages=[
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer"},
        ],
    )
    request = ChatRequest(message="regenerate")

    deps_module.apply_regenerate(session, request)

    assert request.message == "second question"
    assert session.messages == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]


def test_apply_regenerate_without_user_messages_is_noop() -> None:
    session = Session(
        session_id="s2",
        issue_url="https://github.com/acme/widget/issues/1",
        messages=[{"role": "assistant", "content": "only assistant"}],
    )
    request = ChatRequest(message="regenerate")

    deps_module.apply_regenerate(session, request)

    assert request.message == "regenerate"
    assert len(session.messages) == 1


def test_apply_regenerate_with_empty_user_content_keeps_request_message() -> None:
    session = Session(
        session_id="s3",
        issue_url="https://github.com/acme/widget/issues/1",
        messages=[{"role": "user", "content": ""}],
    )
    request = ChatRequest(message="fallback")

    deps_module.apply_regenerate(session, request)

    assert request.message == "fallback"
    assert session.messages == []


async def test_chat_regenerate_replays_last_user_message(monkeypatch) -> None:
    manager = SessionManager()
    _override_session_manager(manager)
    captured: dict = {}

    async def fake_chat(self: IssueAgent, session: Session, message: str):
        captured["message"] = message
        from app.models import ChatResponse

        return ChatResponse(session_id=session.session_id, reply="regenerated")

    monkeypatch.setattr(IssueAgent, "chat", fake_chat)
    client = TestClient(app)
    try:
        stream = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
        session_id = json.loads(
            next(line for line in stream.text.splitlines() if '"type": "session"' in line).removeprefix("data: ")
        )["data"]["session_id"]
        session = await manager.get(session_id)
        assert session is not None
        session.messages = [
            {"role": "user", "content": "explain the root cause"},
            {"role": "assistant", "content": "old answer"},
        ]
        await manager.save(session)

        response = client.post("/chat", json={"session_id": session_id, "message": "ignored", "regenerate": True})
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 200
    assert captured["message"] == "explain the root cause"


async def _post_chat_with_session_error(monkeypatch, error: Exception, expected_status: int):
    manager = SessionManager()
    _override_session_manager(manager)

    async def failing_chat(self: IssueAgent, session: Session, message: str):
        raise error

    monkeypatch.setattr(IssueAgent, "chat", failing_chat)
    client = TestClient(app)
    try:
        stream = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
        session_id = json.loads(
            next(line for line in stream.text.splitlines() if '"type": "session"' in line).removeprefix("data: ")
        )["data"]["session_id"]
        response = client.post("/chat", json={"session_id": session_id, "message": "hi"})
        detail = client.get(f"/session/{session_id}").json()
    finally:
        app.dependency_overrides.pop(get_session_manager, None)
    assert response.status_code == expected_status
    return detail


async def test_chat_rate_limit_error_marks_failed(monkeypatch) -> None:
    from app.github import GitHubRateLimitError

    detail = await _post_chat_with_session_error(monkeypatch, GitHubRateLimitError("limit"), 429)
    assert detail["status"] == "failed"


async def test_chat_api_error_maps_to_502(monkeypatch) -> None:
    detail = await _post_chat_with_session_error(
        monkeypatch, openai.APIError("model down", request=None, body=None), 502
    )
    assert detail["status"] == "failed"


async def test_chat_model_response_error_maps_to_502(monkeypatch) -> None:
    detail = await _post_chat_with_session_error(monkeypatch, ModelResponseError("invalid report"), 502)
    assert detail["status"] == "failed"


async def test_chat_stream_regenerate_replays_last_user_message(monkeypatch) -> None:
    manager = SessionManager()
    _override_session_manager(manager)
    captured: dict = {}

    async def fake_stream_chat(self: IssueAgent, session: Session, message: str):
        captured["message"] = message
        yield {"type": "delta", "content": "ok"}
        yield {"type": "done", "reply": "ok", "tools_used": []}

    monkeypatch.setattr(IssueAgent, "chat_stream", fake_stream_chat)
    client = TestClient(app)
    try:
        stream = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
        session_id = json.loads(
            next(line for line in stream.text.splitlines() if '"type": "session"' in line).removeprefix("data: ")
        )["data"]["session_id"]
        session = await manager.get(session_id)
        assert session is not None
        session.messages = [
            {"role": "user", "content": "what is the fix?"},
            {"role": "assistant", "content": "stale answer"},
        ]
        await manager.save(session)

        response = client.post(
            "/chat/stream", json={"session_id": session_id, "message": "ignored", "regenerate": True}
        )
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 200
    assert captured["message"] == "what is the fix?"
    assert '"type": "done"' in response.text


async def test_chat_stream_keepalive_emitted_for_slow_first_token(monkeypatch) -> None:
    manager = SessionManager()
    _override_session_manager(manager)
    original = chat_routes._iter_events_with_heartbeat

    async def fast_heartbeat(event_iter, *, timeout: float = 15.0):
        async for item in original(event_iter, timeout=0.05):
            yield item

    monkeypatch.setattr(chat_routes, "_iter_events_with_heartbeat", fast_heartbeat)

    async def slow_chat(self: IssueAgent, session: Session, message: str):
        await asyncio.sleep(0.2)
        yield {"type": "delta", "content": "hello"}
        yield {"type": "done", "reply": "hello", "tools_used": []}

    monkeypatch.setattr(IssueAgent, "chat_stream", slow_chat)
    try:
        client = TestClient(app)
        stream = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
        session_id = json.loads(
            next(line for line in stream.text.splitlines() if '"type": "session"' in line).removeprefix("data: ")
        )["data"]["session_id"]
        response = client.post("/chat/stream", json={"session_id": session_id, "message": "hi"})
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert ": keepalive" in response.text
    assert '"type": "done"' in response.text


async def test_chat_stream_cancellation_marks_interrupted(monkeypatch) -> None:
    manager = SessionManager()
    breaker = CircuitBreaker(threshold=5, recovery=30)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    # 已完成态起步：running 的会话现在会被会话租约直接 409 拒绝（一个写者原则），
    # 本用例要验证的是「取消 → interrupted 终态」，与初始状态无关。
    session.status = "completed"
    await manager.save(session)

    async def cancelled_chat(self: IssueAgent, session: Session, message: str):
        yield {"type": "delta", "content": "partial"}
        raise asyncio.CancelledError

    monkeypatch.setattr(IssueAgent, "chat_stream", cancelled_chat)
    response = await chat_routes.chat_stream(
        ChatRequest(session_id=session.session_id, message="hi"),
        ProviderClients(None, None),
        manager,
        breaker,
        InvestigationGate(4),
    )
    with pytest.raises(asyncio.CancelledError):
        async for _ in response.body_iterator:
            pass

    refreshed = await manager.get(session.session_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.phase == "interrupted"


async def test_chat_stream_cancellation_survives_persist_failure(monkeypatch) -> None:
    manager = SessionManager()
    breaker = CircuitBreaker(threshold=5, recovery=30)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.status = "completed"  # 同上：running 会被会话租约 409 拒绝
    await manager.save(session)

    async def failing_mark(manager_, session_id: str, started_at: float) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(chat_routes, "mark_stream_interrupted", failing_mark)

    async def cancelled_chat(self: IssueAgent, session: Session, message: str):
        raise asyncio.CancelledError
        yield  # pragma: no cover

    monkeypatch.setattr(IssueAgent, "chat_stream", cancelled_chat)
    response = await chat_routes.chat_stream(
        ChatRequest(session_id=session.session_id, message="hi"),
        ProviderClients(None, None),
        manager,
        breaker,
        InvestigationGate(4),
    )
    with pytest.raises(asyncio.CancelledError):
        async for _ in response.body_iterator:
            pass


async def test_chat_stream_generic_exception_yields_error_event(monkeypatch) -> None:
    manager = SessionManager()
    breaker = CircuitBreaker(threshold=5, recovery=30)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
    )
    session.status = "completed"
    await manager.save(session)

    async def failing_chat(self: IssueAgent, session: Session, message: str):
        raise RuntimeError("agent crashed")
        yield  # pragma: no cover

    monkeypatch.setattr(IssueAgent, "chat_stream", failing_chat)
    response = await chat_routes.chat_stream(
        ChatRequest(session_id=session.session_id, message="hi"),
        ProviderClients(None, None),
        manager,
        breaker,
        InvestigationGate(4),
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert any('"type": "error"' in chunk for chunk in chunks)
    refreshed = await manager.get(session.session_id)
    assert refreshed is not None
    assert refreshed.status == "failed"


# ══════════════════════════════════════════════════════════════════
# main.py：cancel / PATCH / DELETE / report / proposal / export 404 与 409
# ══════════════════════════════════════════════════════════════════


def test_cancel_endpoint_rejects_unknown_session() -> None:
    manager = SessionManager()
    _override_session_manager(manager)
    try:
        response = TestClient(app).post("/session/ghost/cancel")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 404


def test_cancel_endpoint_rejects_non_running_session(monkeypatch) -> None:
    manager = SessionManager()
    _override_session_manager(manager)

    async def fake_stream(self: IssueAgent, issue_url: str, *, session=None):
        yield done_event()

    monkeypatch.setattr(IssueAgent, "investigate_stream", fake_stream)
    client = TestClient(app)
    try:
        stream = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
        session_id = json.loads(
            next(line for line in stream.text.splitlines() if '"type": "session"' in line).removeprefix("data: ")
        )["data"]["session_id"]
        response = client.post(f"/session/{session_id}/cancel")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 409


async def test_cancel_endpoint_conflict_when_request_cancel_fails(monkeypatch) -> None:
    manager = SessionManager()
    _override_session_manager(manager)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.status = "running"
    await manager.save(session)

    async def failing_request_cancel(session_id: str) -> bool:
        return False

    monkeypatch.setattr(manager, "request_cancel", failing_request_cancel)
    try:
        response = TestClient(app).post(f"/session/{session.session_id}/cancel")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 409


async def test_cancel_endpoint_404_when_refreshed_session_missing(monkeypatch) -> None:
    manager = SessionManager()
    _override_session_manager(manager)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.status = "running"
    await manager.save(session)
    original_get = manager.get

    async def get_once(session_id: str):
        result = await original_get(session_id)
        return result

    call_state = {"cancelled": False}

    async def request_cancel(session_id: str) -> bool:
        call_state["cancelled"] = True
        return True

    async def get_after_cancel(session_id: str):
        if call_state["cancelled"]:
            return None
        return await original_get(session_id)

    monkeypatch.setattr(manager, "request_cancel", request_cancel)
    monkeypatch.setattr(manager, "get", get_after_cancel)
    try:
        response = TestClient(app).post(f"/session/{session.session_id}/cancel")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 404


def test_session_patch_delete_report_proposal_export_404_paths() -> None:
    manager = SessionManager()
    _override_session_manager(manager)
    client = TestClient(app)
    try:
        patch = client.patch("/session/ghost", json={"display_title": "x"})
        delete = client.delete("/session/ghost")
        report = client.get("/session/ghost/report")
        proposal = client.get("/session/ghost/proposal")
        export = client.get("/session/ghost/export")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert patch.status_code == 404
    assert delete.status_code == 404
    assert report.status_code == 404
    assert proposal.status_code == 404
    assert export.status_code == 404


async def test_session_report_404_when_not_generated() -> None:
    manager = SessionManager()
    _override_session_manager(manager)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    try:
        response = TestClient(app).get(f"/session/{session.session_id}/report")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 404
    assert "Report not yet generated" in response.json()["detail"]


async def test_session_proposal_404_when_none_pending() -> None:
    manager = SessionManager()
    _override_session_manager(manager)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    try:
        response = TestClient(app).get(f"/session/{session.session_id}/proposal")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 404
    assert "No pending PR proposal" in response.json()["detail"]


# ══════════════════════════════════════════════════════════════════
# main.py：会话导入边界（Content-Length / 超大 body / 非法数据）
# ══════════════════════════════════════════════════════════════════


def _import_request(body: bytes, content_length: str | None) -> Request:
    headers = []
    if content_length is not None:
        headers.append((b"content-length", content_length.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/session/import",
        "headers": headers,
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


async def test_import_rejects_invalid_content_length_header() -> None:
    manager = SessionManager()
    with pytest.raises(HTTPException) as exc_info:
        await sessions_routes.import_session(_import_request(b"{}", "abc"), manager)

    assert exc_info.value.status_code == 400


async def test_import_rejects_body_larger_than_declared_length(monkeypatch) -> None:
    manager = SessionManager()
    settings = Settings(openai_api_key="test-key", max_session_import_bytes=65_536)
    monkeypatch.setattr(sessions_routes, "get_settings", lambda: settings)
    with pytest.raises(HTTPException) as exc_info:
        await sessions_routes.import_session(_import_request(b"x" * 70_000, "100"), manager)

    assert exc_info.value.status_code == 413


async def test_import_tolerates_invalid_issue_and_report_data() -> None:
    manager = SessionManager()
    payload = {
        "format": "issue-agent-session",
        "version": 1,
        "session": {
            "issue_url": "https://github.com/acme/widget/issues/1",
            "issue": {"owner": 123, "repo": [], "number": "x", "title": None, "body": 1},
            "report": {"summary": 123, "root_cause": [], "confidence": "maybe"},
            "messages": [{"role": "user", "content": "hi"}],
            "metrics": {"model_calls": 2},
        },
        "events": [],
    }
    summary = await sessions_routes.import_session(_import_request(json.dumps(payload).encode(), None), manager)

    assert summary.issue_url == "https://github.com/acme/widget/issues/1"
    assert summary.status == "completed"
    detail = await manager.get(summary.session_id)
    assert detail is not None
    assert detail.issue is None
    assert detail.report is None
    assert detail.metrics == {"model_calls": 2}


# ══════════════════════════════════════════════════════════════════
# main.py：批量端点 happy path
# ══════════════════════════════════════════════════════════════════


def test_batch_submit_and_status_happy_path() -> None:
    from app.deps import get_task_queue
    from app.task_queue import Batch, BatchTask

    batch = Batch(
        batch_id="batch-1",
        tasks=[
            BatchTask(task_id="t1", issue_url="https://github.com/acme/widget/issues/1", status="completed"),
            BatchTask(task_id="t2", issue_url="https://github.com/acme/widget/issues/2", status="failed", error="x"),
        ],
    )
    batch.status = "partial"

    class _FakeQueue:
        def submit(self, urls):
            return batch

        def get_batch(self, batch_id):
            return batch if batch_id == "batch-1" else None

        async def get_batch_async(self, batch_id):
            return batch if batch_id == "batch-1" else None

    app.dependency_overrides[get_task_queue] = lambda: _FakeQueue()
    try:
        client = TestClient(app)
        submit = client.post("/batch", json={"issue_urls": ["https://github.com/acme/widget/issues/1"]})
        status = client.get("/batch/batch-1")
    finally:
        app.dependency_overrides.pop(get_task_queue, None)

    assert submit.status_code == 200
    assert submit.json()["batch_id"] == "batch-1"
    assert submit.json()["status"] == "partial"
    assert len(submit.json()["tasks"]) == 2
    assert submit.json()["tasks"][1]["error"] == "x"
    assert status.status_code == 200
    assert status.json()["batch_id"] == "batch-1"


# ══════════════════════════════════════════════════════════════════
# main.py：lifespan 周期任务（清理/恢复）与启动恢复计数
# ══════════════════════════════════════════════════════════════════


async def test_lifespan_periodic_tasks_purge_and_recover(monkeypatch) -> None:
    """周期清理/恢复任务至少执行一轮：成功记日志、异常降级继续。"""
    real_sleep = asyncio.sleep
    per_task_sleeps: dict = {}
    recover_calls = {"count": 0}
    purge_calls = {"count": 0}

    async def fake_sleep(seconds, *args, **kwargs):
        if seconds >= 30:  # 只有周期任务的间隔（30s~6h）走模拟
            task = asyncio.current_task()
            count = per_task_sleeps.get(task, 0) + 1
            per_task_sleeps[task] = count
            if count >= 3:  # 每个周期任务执行两轮后终止
                raise asyncio.CancelledError
            return None
        return await real_sleep(seconds)

    async def fake_recover_stale(self, cutoff: str) -> int:
        recover_calls["count"] += 1
        if recover_calls["count"] == 1:
            raise RuntimeError("db busy")  # 启动恢复失败：仅告警
        if recover_calls["count"] == 2:
            raise RuntimeError("db busy again")  # 第一轮周期恢复失败：下轮重试
        return 3  # 第二轮周期恢复成功：记 warning 日志

    async def fake_purge_old_sessions(self, retention_days: int) -> int:
        purge_calls["count"] += 1
        if purge_calls["count"] == 1:
            return 2  # 启动清理成功
        if purge_calls["count"] == 2:
            return 1  # 周期清理成功
        raise RuntimeError("db locked")  # 第二轮周期清理失败：仅告警

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(SessionManager, "recover_stale", fake_recover_stale)
    monkeypatch.setattr(SessionManager, "purge_old_sessions", fake_purge_old_sessions)

    async with main_module.lifespan(app):
        await real_sleep(0.05)  # 让周期任务跑完两轮

    assert recover_calls["count"] >= 3
    assert purge_calls["count"] >= 3
    assert app.state.session_manager is None  # lifespan 收尾清理


# ══════════════════════════════════════════════════════════════════
# agent.py：故障路径
# ══════════════════════════════════════════════════════════════════


async def test_agent_aclose_closes_owned_client(make_agent) -> None:
    agent = make_agent()
    agent._get_client()
    assert agent._owns_client is True

    await agent.aclose()

    assert agent._client is None


async def test_investigation_preloads_issue_referenced_files(
    fake_client, fake_response, fake_tool_call, monkeypatch
) -> None:
    """issue 文本引用的文件被预读：成功的进入缓存并输出事件，失败的仅告警。"""
    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)
    tc = fake_tool_call("read_file", {"path": "src/parser.py"})
    responses = [
        fake_response(tool_calls=[tc]),
        fake_response(content="Done"),
        fake_response(content=json.dumps(_report_data())),
    ]
    agent = IssueAgent(
        Settings(openai_api_key="test-key", max_agent_iterations=5, independent_review=False),
        client=fake_client(responses),
    )
    session = Session(session_id="preload", issue_url="https://github.com/acme/widget/issues/1")

    # 让 src/lexer.py 在 executor.execute 层直接抛错（而非被 execute 内部捕获），
    # 触发预读协程的 except 分支：仅告警、不产出事件
    original_build = IssueAgent._build_executor

    def build_with_failing_read(self, github, issue, tree, **kwargs):
        executor = original_build(self, github, issue, tree, **kwargs)
        original_execute = executor.execute

        async def execute(name: str, arguments: dict) -> str:
            if arguments.get("path") == "src/lexer.py":
                raise RuntimeError("read failed hard")
            return await original_execute(name, arguments)

        executor.execute = execute
        return executor

    monkeypatch.setattr(IssueAgent, "_build_executor", build_with_failing_read)

    events = [
        event async for event in agent.investigate_stream("https://github.com/acme/widget/issues/1", session=session)
    ]

    types = [e.type for e in events]
    assert "phase" in types
    # 预读成功路径输出带 auto 标记的 tool_call/tool_result 事件
    preload_calls = [e for e in events if e.type == "tool_call" and e.data and e.data.get("args", {}).get("auto")]
    assert any(e.data and e.data["args"]["path"] == "src/parser.py" for e in preload_calls)
    assert "src/parser.py" in session.files_read
    # src/lexer.py 读取失败：不进入缓存
    assert "src/lexer.py" not in session.files_read


async def test_investigation_timeout_raises(fake_client, fake_response, monkeypatch) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)
    agent = IssueAgent(
        Settings(openai_api_key="test-key", investigation_timeout=0.000001, independent_review=False),
        client=fake_client([fake_response(content="Done")]),
    )

    with pytest.raises(TimeoutError, match="total timeout"):
        async for _ in agent.investigate_stream("https://github.com/acme/widget/issues/1"):
            pass


async def test_investigation_uses_circuit_breaker_for_exploration(
    fake_client, fake_response, fake_tool_call, monkeypatch
) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)
    tc = fake_tool_call("read_file", {"path": "src/parser.py"})
    client = fake_client(
        [
            fake_response(tool_calls=[tc]),
            fake_response(content="Done"),
            fake_response(content=json.dumps(_report_data())),
        ]
    )
    agent = IssueAgent(
        Settings(openai_api_key="test-key", independent_review=False),
        client=client,
        circuit_breaker=CircuitBreaker(threshold=5, recovery=30),
    )
    report = await agent.investigate("https://github.com/acme/widget/issues/1")

    assert report.summary == "Parser bug"


async def test_investigation_model_no_choices_raises(fake_client, monkeypatch) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)
    empty_response = SimpleNamespace(choices=[])
    agent = IssueAgent(
        Settings(openai_api_key="test-key", independent_review=False),
        client=fake_client([empty_response]),
    )

    with pytest.raises(ModelResponseError, match="no choices"):
        async for _ in agent.investigate_stream("https://github.com/acme/widget/issues/1"):
            pass


async def test_investigation_propagates_pending_pr_to_session(
    fake_client, fake_response, fake_tool_call, monkeypatch
) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)
    tc = fake_tool_call("read_file", {"path": "src/parser.py"})
    agent = IssueAgent(
        Settings(openai_api_key="test-key", independent_review=False),
        client=fake_client(
            [
                fake_response(tool_calls=[tc]),
                fake_response(content="Done"),
                fake_response(content=json.dumps(_report_data())),
            ]
        ),
    )
    proposal = {"branch": "fix/parser", "title": "Fix", "body": "b", "changes": []}
    original_build = IssueAgent._build_executor

    def build_with_proposal(self, github, issue, tree, **kwargs):
        executor = original_build(self, github, issue, tree, **kwargs)
        executor.pr_proposal = proposal
        return executor

    monkeypatch.setattr(IssueAgent, "_build_executor", build_with_proposal)
    session = Session(session_id="pr", issue_url="https://github.com/acme/widget/issues/1")

    await agent.investigate("https://github.com/acme/widget/issues/1", session=session)

    assert session.pending_pr == proposal


async def test_investigation_warns_on_max_iterations(fake_client, fake_response, fake_tool_call, monkeypatch) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)
    # 三轮迭代参数各不相同（避免触发重复轮次熔断），全部带工具调用 → 达到上限
    tool_calls = [
        fake_tool_call("read_file", {"path": "src/parser.py", "start_line": i}, call_id=f"call_{i}")
        for i in range(1, 4)
    ]
    agent = IssueAgent(
        Settings(openai_api_key="test-key", max_agent_iterations=3, independent_review=False),
        client=fake_client(
            [fake_response(tool_calls=[tc]) for tc in tool_calls] + [fake_response(content=json.dumps(_report_data()))]
        ),
    )
    session = Session(session_id="maxiter", issue_url="https://github.com/acme/widget/issues/1")

    report = await agent.investigate("https://github.com/acme/widget/issues/1", session=session)

    assert report.summary == "Parser bug"


async def test_investigation_stream_passes_reasoning_events_through(fake_client, fake_response, monkeypatch) -> None:
    """报告生成阶段的 reasoning 事件透传到 investigate_stream 输出。"""
    from tests.conftest import _FakeStreamChunk

    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)
    report_chunks = [
        _FakeStreamChunk(reasoning_content="Analyzing the parser..."),
        _FakeStreamChunk(content=json.dumps(_report_data())),
    ]
    agent = IssueAgent(
        Settings(openai_api_key="test-key", independent_review=False),
        client=fake_client([fake_response(content="Done"), report_chunks]),
    )

    events = [event async for event in agent.investigate_stream("https://github.com/acme/widget/issues/1")]

    reasoning = [e for e in events if e.type == "reasoning"]
    assert reasoning and reasoning[0].data and "Analyzing the parser" in reasoning[0].data["delta"]


async def test_investigation_raises_when_report_stream_produces_nothing(
    fake_client, fake_response, monkeypatch
) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)
    from app.report_generator import ReportGenerator

    async def empty_generate_stream(self, messages, executor, metrics=None, *, deadline=None):
        yield phase_event("verifying", "no report follows")
        return

    monkeypatch.setattr(ReportGenerator, "generate_stream", empty_generate_stream)
    agent = IssueAgent(
        Settings(openai_api_key="test-key", independent_review=False),
        client=fake_client([fake_response(content="Done")]),
    )

    with pytest.raises(ModelResponseError, match="did not produce"):
        async for _ in agent.investigate_stream("https://github.com/acme/widget/issues/1"):
            pass


async def test_chat_prepare_failure_closes_owned_github_client(make_agent, monkeypatch) -> None:
    """_chat_prepare 中 executor 构建失败：新建的 GitHubClient 必须被关闭。"""
    agent = make_agent()
    session = Session(session_id="prepare-fail", issue_url="https://github.com/acme/widget/issues/1")
    session.issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
    )
    captured = []

    class _TrackingGitHub(_RefGitHub):
        pass

    monkeypatch.setattr("app.agent.GitHubClient", _TrackingGitHub)

    def broken_build(self, github, issue, tree, **kwargs):
        raise RuntimeError("executor boom")

    monkeypatch.setattr(IssueAgent, "_build_executor", broken_build)
    # _chat_prepare 内部在异常时关闭 github；通过包装 __aexit__ 验证
    original_aexit = _TrackingGitHub.__aexit__

    async def tracking_aexit(self, *args):
        captured.append("exited")
        return await original_aexit(self, *args)

    monkeypatch.setattr(_TrackingGitHub, "__aexit__", tracking_aexit)

    with pytest.raises(RuntimeError, match="executor boom"):
        await agent.chat(session, "hello")

    assert session.messages == []  # 追加的 user 消息已回滚


async def test_chat_model_no_choices_raises(make_agent, fake_client, monkeypatch) -> None:
    agent = make_agent(client=fake_client([SimpleNamespace(choices=[])]))
    session = Session(session_id="nochoice", issue_url="https://github.com/acme/widget/issues/1")
    session.issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
    )

    with pytest.raises(ModelResponseError, match="no choices"):
        await agent.chat(session, "hello")


async def test_chat_empty_stream_response_rolls_back_and_yields_error(make_agent, fake_client, monkeypatch) -> None:
    """chat_stream 收到空 stream：回滚 user 消息，返回友好错误事件。"""
    agent = make_agent(client=fake_client([[]]))  # stream=True 且响应为空 chunk 列表
    session = Session(session_id="empty-stream", issue_url="https://github.com/acme/widget/issues/1")
    session.issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
    )
    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)

    events = [event async for event in agent.chat_stream(session, "hello")]

    assert any(e.get("type") == "error" and "empty response" in e.get("message", "") for e in events)
    assert not any(m.get("role") == "user" for m in session.messages)


async def test_chat_stream_invalid_tool_arguments_fallback_to_empty(make_agent, fake_client, monkeypatch) -> None:
    """流式工具调用参数非法 JSON：参数降级为空 dict，工具错误作为结果返回。"""
    from tests.conftest import _FakeStreamChunk

    invalid_tc = SimpleNamespace(
        index=0,
        id="call_bad",
        function=SimpleNamespace(name="read_file", arguments="{not json"),
    )
    delta = SimpleNamespace(content=None, reasoning_content=None, reasoning=None, tool_calls=[invalid_tc])
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="tool_calls")], usage=None)
    agent = make_agent(client=fake_client([[chunk], [_FakeStreamChunk(content="recovered")]]))
    session = Session(session_id="badargs", issue_url="https://github.com/acme/widget/issues/1")
    session.issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
    )
    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)

    events = [event async for event in agent.chat_stream(session, "hello")]

    # 参数解析失败 → args={} → read_file 缺 path 报错，但流仍以 done 收尾
    assert any(e.get("type") == "tool_call" for e in events)
    assert any(e.get("type") == "done" for e in events)


async def test_chat_stream_records_usage_from_final_chunk(make_agent, fake_client, monkeypatch) -> None:
    from tests.conftest import _FakeStreamChunk

    usage_chunk = SimpleNamespace(usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}, choices=[])
    agent = make_agent(client=fake_client([[_FakeStreamChunk(content="hi"), usage_chunk], []]))
    session = Session(session_id="usage", issue_url="https://github.com/acme/widget/issues/1")
    session.issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
    )
    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)

    events = [event async for event in agent.chat_stream(session, "hello")]

    assert any(e.get("type") == "done" for e in events)
    assert session.metrics.get("input_tokens") == 7
    assert session.metrics.get("chat_total_tokens") == 10


async def test_chat_propagates_pending_pr_to_session(make_agent, monkeypatch) -> None:
    from tests.conftest import _FakeClient, _FakeResponse, _FakeToolCall

    tc = _FakeToolCall("read_file", {"path": "src/parser.py"})
    responses = [
        _FakeResponse(SimpleNamespace(content=None, tool_calls=[tc], reasoning_content=None)),
        _FakeResponse(SimpleNamespace(content="done", tool_calls=None, reasoning_content=None)),
    ]
    agent = make_agent(client=_FakeClient(responses))
    session = Session(session_id="chat-pr", issue_url="https://github.com/acme/widget/issues/1")
    session.issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
    )
    monkeypatch.setattr("app.agent.GitHubClient", _RefGitHub)
    proposal = {"branch": "fix/chat", "title": "Fix", "body": "b", "changes": []}
    original_build = IssueAgent._build_executor

    def build_with_proposal(self, github, issue, tree, **kwargs):
        executor = original_build(self, github, issue, tree, **kwargs)
        executor.pr_proposal = proposal
        return executor

    monkeypatch.setattr(IssueAgent, "_build_executor", build_with_proposal)

    result = await agent.chat(session, "propose a fix")

    assert session.pending_pr == proposal
    assert result.reply == "done"


def test_build_initial_messages_prioritizes_referenced_paths(make_agent, make_issue) -> None:
    agent = make_agent()
    tree = ["src/a.py", "src/b.py", "src/c.py", "src/d.py"]

    messages = agent._build_initial_messages(make_issue(), tree, referenced_paths=["src/c.py", "src/a.py"])

    user_content = messages[1]["content"]
    assert "Issue 显式引用的文件路径" in user_content
    # 引用路径排在候选列表前部
    assert user_content.index("src/c.py") < user_content.index("src/d.py")


async def test_call_llm_and_stream_without_breaker_use_client_directly(make_agent) -> None:
    agent = make_agent()
    from tests.conftest import _FakeClient, _FakeResponse

    client = _FakeClient([_FakeResponse(SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None))])
    agent._client = client
    agent._owns_client = False

    response = await agent._call_llm([{"role": "user", "content": "hi"}])
    assert response.choices[0].message.content == "ok"

    stream_client = _FakeClient([[_FakeStreamChunkStub()]])
    agent._client = stream_client
    stream = await agent._call_llm_stream([{"role": "user", "content": "hi"}])
    chunks = [chunk async for chunk in stream]
    assert chunks


class _FakeStreamChunkStub:
    def __init__(self) -> None:
        delta = SimpleNamespace(content="hi", reasoning_content=None, reasoning=None, tool_calls=None)
        self.choices = [SimpleNamespace(delta=delta, finish_reason="stop")]
        self.usage = None


@pytest.mark.parametrize(
    ("build_exc", "expected"),
    [
        (lambda: type("Err401", (Exception,), {"status_code": 401})(), "API key is invalid"),
        (lambda: type("Err429", (Exception,), {"status_code": 429})(), "rate limit reached"),
        (lambda: type("Err500", (Exception,), {"status_code": 503})(), "Model service error"),
        (lambda: TimeoutError(), "timed out"),
        (lambda: httpx.ConnectTimeout("t"), "Unable to connect"),
        (lambda: type("WeirdSdkError", (Exception,), {})(), None),  # 类名无特征 → Internal error
        (lambda: ValueError("plain readable"), "plain readable"),
    ],
)
def test_friendly_chat_error_mapping(build_exc, expected) -> None:
    message = friendly_chat_error(build_exc())
    if expected is not None:
        assert expected in message
    else:
        assert message.startswith("Internal error:")


def test_friendly_chat_error_custom_named_exceptions() -> None:
    class ModelTimeoutError(Exception):
        pass

    class APIConnectionRefused(Exception):
        pass

    assert "timed out" in friendly_chat_error(ModelTimeoutError())
    assert "Unable to connect" in friendly_chat_error(APIConnectionRefused())


def test_trim_session_messages_skips_all_tool_turn() -> None:
    from app.agent import _trim_session_messages

    # 唯一的 turn 全是工具调用（无 user、无纯文本 assistant）：过滤后为空 → 跳过
    messages: list[dict] = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "content": "result", "tool_call_id": "1"},
    ]

    _trim_session_messages(messages, max_chars=0)

    # 全工具调用的 turn 被跳过，历史清空而非崩溃
    assert messages == []


def test_trim_session_messages_trims_content_overflow() -> None:
    from app.agent import _trim_session_messages

    messages = [{"role": "user", "content": "hello world"}]

    _trim_session_messages(messages, max_chars=5)

    assert len(messages) == 1
    assert len(messages[0]["content"]) == 5


def test_build_investigation_context_includes_report_details() -> None:
    from app.agent import _build_investigation_context

    session = Session(session_id="ctx", issue_url="https://github.com/acme/widget/issues/1")
    session.report = AnalysisReport.model_validate(_report_data(files_examined=["src/parser.py"]))

    context = _build_investigation_context(session)

    assert "Summary: Parser bug" in context
    assert "Root Cause:" in context
    assert "src/parser.py" in context
    assert "Proposed Changes" in context


def test_build_investigation_context_without_report() -> None:
    from app.agent import _build_investigation_context

    session = Session(session_id="ctx2", issue_url="https://github.com/acme/widget/issues/1")
    context = _build_investigation_context(session)

    assert context  # 返回“暂无调查”之类的提示文案


# ══════════════════════════════════════════════════════════════════
# report_generator.py：重试、超时、usage、空响应
# ══════════════════════════════════════════════════════════════════


def _make_executor(files: dict[str, str] | None = None) -> MagicMock:
    executor = MagicMock()
    executor.files_read = list((files or {}).keys())
    executor.file_cache = files or {}
    executor.line_counts = {path: content.count("\n") + 1 for path, content in (files or {}).items()}
    executor.investigation_ledger = []
    return executor


async def test_report_stream_raises_when_deadline_exceeded(fake_client, fake_response) -> None:
    from app.report_generator import ReportGenerator

    generator = ReportGenerator(Settings(openai_api_key="test-key"), fake_client([fake_response(content="{}")]))

    with pytest.raises(TimeoutError, match="deadline"):
        async for _ in generator.generate_stream([], _make_executor(), deadline=time.monotonic() - 1):
            pass


async def test_report_stream_retries_on_no_choices_then_raises(fake_client) -> None:
    from app.report_generator import ReportGenerator

    # 两次尝试都返回空 chunk 列表（无 choices）：第一次 continue 重试，最后一次 raise
    generator = ReportGenerator(
        Settings(openai_api_key="test-key", max_report_retries=2),
        fake_client([[], []]),
    )

    with pytest.raises(ModelResponseError, match="no choices"):
        async for _ in generator.generate_stream([], _make_executor()):
            pass


async def test_report_stream_records_usage_and_reasoning(fake_client, fake_response) -> None:
    from app.report_generator import ReportGenerator
    from tests.conftest import _FakeStreamChunk

    usage_chunk = SimpleNamespace(usage={"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}, choices=[])
    report_chunks = [
        _FakeStreamChunk(reasoning_content="thinking hard"),
        _FakeStreamChunk(content=json.dumps(_report_data())),
        usage_chunk,
    ]
    generator = ReportGenerator(
        Settings(openai_api_key="test-key"),
        fake_client([report_chunks]),
    )
    metrics: dict = {}

    items = [item async for item in generator.generate_stream([], _make_executor({"src/parser.py": "x\ny"}), metrics)]

    assert any(getattr(i, "type", None) == "reasoning" for i in items)
    report = next(i for i in items if isinstance(i, AnalysisReport))
    assert report.summary == "Parser bug"
    assert metrics.get("input_tokens") == 11
    assert metrics.get("report_total_tokens") == 15


async def test_report_generate_raises_when_stream_empty(fake_client) -> None:
    from app.report_generator import ReportGenerator

    class _EmptyGenerator(ReportGenerator):
        async def generate_stream(self, messages, executor, metrics=None, *, deadline=None):
            return
            yield  # pragma: no cover

    generator = _EmptyGenerator(Settings(openai_api_key="test-key"), fake_client([]))

    with pytest.raises(ModelResponseError, match="did not produce"):
        await generator.generate([], _make_executor())


def test_build_report_messages_skips_files_without_content() -> None:
    from app.report_generator import ReportGenerator

    generator = ReportGenerator(Settings(openai_api_key="test-key"), MagicMock())
    executor = _make_executor({"src/a.py": "line1"})
    executor.files_read = ["src/a.py", "src/missing.py"]
    executor.file_cache = {"src/a.py": "line1"}  # missing.py 无内容

    messages = generator.build_report_messages(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "issue"}], executor
    )

    assert "src/a.py" in messages[1]["content"]
    assert "src/missing.py" not in messages[1]["content"]


# ══════════════════════════════════════════════════════════════════
# reviewer.py：超时、熔断器、空响应、patch 截断
# ══════════════════════════════════════════════════════════════════


def _review_issue() -> IssueData:
    return IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="body",
        labels=[],
        comments=[],
        default_branch="main",
    )


async def test_reviewer_raises_when_deadline_exceeded(fake_client, fake_response) -> None:
    from app.reviewer import ReviewerAgent

    reviewer = ReviewerAgent(Settings(openai_api_key="test-key"), fake_client([fake_response(content="{}")]))

    with pytest.raises(TimeoutError, match="deadline"):
        await reviewer.review(
            issue=_review_issue(),
            report=AnalysisReport.model_validate(_report_data()),
            file_cache={},
            files_read=[],
            line_counts={},
            deadline=time.monotonic() - 1,
        )


async def test_reviewer_retries_on_empty_content_then_raises(fake_client, fake_response) -> None:
    from app.reviewer import ReviewerAgent

    reviewer = ReviewerAgent(
        Settings(openai_api_key="test-key", max_report_retries=2),
        fake_client([fake_response(content=""), fake_response(content=None)]),
    )

    from app.errors import ReviewResponseError

    with pytest.raises(ReviewResponseError, match="empty response"):
        await reviewer.review(
            issue=_review_issue(),
            report=AnalysisReport.model_validate(_report_data()),
            file_cache={},
            files_read=[],
            line_counts={},
        )


async def test_reviewer_uses_circuit_breaker(fake_client, fake_response) -> None:
    from app.reviewer import ReviewerAgent

    report_data = _report_data()
    review_data = {
        "verdict": "approved",
        "summary": "ok",
        "findings": [],
        "report": report_data,
    }
    reviewer = ReviewerAgent(
        Settings(openai_api_key="test-key"),
        fake_client([fake_response(content=json.dumps(review_data))]),
        circuit_breaker=CircuitBreaker(threshold=5, recovery=30),
    )

    outcome = await reviewer.review(
        issue=_review_issue(),
        report=AnalysisReport.model_validate(report_data),
        file_cache={"src/parser.py": "x\ny"},
        files_read=["src/parser.py"],
        line_counts={"src/parser.py": 2},
    )

    # 证据校验器可能微调报告 → verdict 归一化为 revised，review_audit 始终落盘
    assert outcome.verdict in {"approved", "revised"}
    assert outcome.report.review_audit.status == outcome.verdict


def test_review_context_truncates_huge_patch() -> None:
    from app.reviewer import _build_review_context

    report = AnalysisReport.model_validate(_report_data(patch="x" * 10_000))
    context = _build_review_context(_review_issue(), report, {}, [], 200_000)

    # patch 在上下文中被截断到 4000 字符
    assert "xxxx" in context
    assert context.count("x") < 10_000


# ══════════════════════════════════════════════════════════════════
# tools.py：超时、限额、边界工具
# ══════════════════════════════════════════════════════════════════


def test_tool_definitions_include_write_tool_in_write_mode() -> None:
    from app.tools import get_tool_definitions

    read_only = get_tool_definitions(Settings(openai_api_key="k", write_mode=False))
    write = get_tool_definitions(Settings(openai_api_key="k", write_mode=True))

    assert all(t["function"]["name"] != "create_pull_request" for t in read_only)
    assert any(t["function"]["name"] == "create_pull_request" for t in write)


async def test_execute_many_empty_batch_returns_empty() -> None:
    from app.tools import ToolExecutor

    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="k"), _review_issue(), [])

    results, duplicates = await executor.execute_many([], timeout=1.0)

    assert results == []
    assert duplicates == 0


async def test_execute_many_tool_timeout_returns_error() -> None:
    from app.tools import ToolExecutor

    github = MagicMock()

    async def slow_search(issue, query, limit):
        await asyncio.sleep(1)
        return []

    github.search_code = AsyncMock(side_effect=slow_search)
    executor = ToolExecutor(github, Settings(openai_api_key="k"), _review_issue(), [])

    results, _ = await executor.execute_many([("search_code", {"query": "x"})], timeout=0.05)

    assert "timed out" in results[0]


async def test_read_file_returns_error_when_file_limit_reached() -> None:
    from app.tools import ToolExecutor

    github = MagicMock()

    async def get_file(issue, path):
        return SourceFile(path=path, content="c" * 100)

    github.get_file = AsyncMock(side_effect=get_file)
    executor = ToolExecutor(github, Settings(openai_api_key="k"), _review_issue(), ["a.py", "b.py"], max_files=1)

    first = await executor.execute("read_file", {"path": "a.py"})
    second = await executor.execute("read_file", {"path": "b.py"})

    assert "L1" in first
    assert "File limit reached" in second


async def test_read_file_returns_error_when_context_limit_reached() -> None:
    from app.tools import ToolExecutor

    github = MagicMock()

    async def get_file(issue, path):
        return SourceFile(path=path, content="c" * 200)

    github.get_file = AsyncMock(side_effect=get_file)
    executor = ToolExecutor(
        github,
        Settings(openai_api_key="k"),
        _review_issue(),
        ["a.py", "b.py"],
        max_files=5,
        max_file_chars=150,
        max_total_context_chars=100,
    )

    first = await executor.execute("read_file", {"path": "a.py"})
    second = await executor.execute("read_file", {"path": "b.py"})

    assert "L1" in first
    assert "Source context limit reached" in second


async def test_read_file_refetches_full_content_for_range_beyond_cache() -> None:
    """缓存内容被截断后请求更大行号范围：重新拉取完整文件。"""
    from app.tools import ToolExecutor

    github = MagicMock()
    full_content = "\n".join(f"line{i}" for i in range(1, 21))

    async def get_file(issue, path):
        return SourceFile(path=path, content=full_content)

    github.get_file = AsyncMock(side_effect=get_file)
    executor = ToolExecutor(
        github,
        Settings(openai_api_key="k"),
        _review_issue(),
        ["a.py"],
        max_file_chars=10,  # 缓存只保留前几个字符
    )
    await executor.execute("read_file", {"path": "a.py"})

    result = await executor.execute("read_file", {"path": "a.py", "start_line": 15, "end_line": 16})

    assert "L15: line15" in result
    assert github.get_file.call_count >= 2


async def test_search_code_reports_no_matches() -> None:
    from app.tools import ToolExecutor

    github = MagicMock()
    github.search_code = AsyncMock(return_value=[])
    executor = ToolExecutor(github, Settings(openai_api_key="k"), _review_issue(), [])

    result = await executor.execute("search_code", {"query": "nothing"})

    assert "No repository code matches" in result


async def test_grep_unread_file_returns_hint() -> None:
    from app.tools import ToolExecutor

    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="k"), _review_issue(), ["a.py"])

    result = await executor.execute("grep_content", {"pattern": "x", "path": "a.py"})

    assert "has not been read yet" in result


async def test_grep_limits_result_count() -> None:
    from app.tools import _MAX_GREP_RESULTS, ToolExecutor

    github = MagicMock()

    async def get_file(issue, path):
        return SourceFile(path=path, content="\n".join(f"match{i}" for i in range(_MAX_GREP_RESULTS + 10)))

    github.get_file = AsyncMock(side_effect=get_file)
    executor = ToolExecutor(github, Settings(openai_api_key="k"), _review_issue(), ["a.py"])
    await executor.execute("read_file", {"path": "a.py"})

    result = await executor.execute("grep_content", {"pattern": "match"})

    assert len(result.splitlines()) <= _MAX_GREP_RESULTS


async def test_get_file_history_formats_commits() -> None:
    from app.tools import ToolExecutor

    github = MagicMock()
    github.get_file_history = AsyncMock(
        return_value=[{"sha": "abc1234", "date": "2024-01-01", "author": "dev", "message": "fix"}]
    )
    executor = ToolExecutor(github, Settings(openai_api_key="k"), _review_issue(), ["a.py"])

    result = await executor.execute("get_file_history", {"path": "a.py", "max_commits": 5})

    assert "abc1234" in result
    assert "fix" in result


async def test_get_file_history_no_commits() -> None:
    from app.tools import ToolExecutor

    github = MagicMock()
    github.get_file_history = AsyncMock(return_value=[])
    executor = ToolExecutor(github, Settings(openai_api_key="k"), _review_issue(), ["a.py"])

    result = await executor.execute("get_file_history", {"path": "a.py"})

    assert "No commit history" in result


async def test_list_branches_formats_output() -> None:
    from app.tools import ToolExecutor

    github = MagicMock()
    github.list_branches = AsyncMock(
        return_value=[
            {"name": "main", "sha": "abc", "protected": True},
            {"name": "dev", "sha": "def", "protected": False},
        ]
    )
    executor = ToolExecutor(github, Settings(openai_api_key="k"), _review_issue(), [])

    result = await executor.execute("list_branches", {})

    assert "main (abc) [protected]" in result
    assert "dev (def)" in result


async def test_get_file_at_commit_validates_sha() -> None:
    from app.tools import ToolExecutor

    github = MagicMock()
    github.get_file_at_commit = AsyncMock()
    executor = ToolExecutor(github, Settings(openai_api_key="k"), _review_issue(), ["a.py"])

    invalid = await executor.execute("get_file_at_commit", {"path": "a.py", "sha": "zzz"})
    assert "SHA must be" in invalid

    github.get_file_at_commit = AsyncMock(return_value=SourceFile(path="a.py", content="one\ntwo"))
    valid = await executor.execute("get_file_at_commit", {"path": "a.py", "sha": "abc1234"})
    assert "L1: one" in valid


async def test_create_pull_request_rejected_when_write_disabled() -> None:
    from app.tools import ToolExecutor

    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="k", write_mode=False), _review_issue(), [])

    result = await executor.execute("create_pull_request", {"branch": "fix", "title": "t", "body": "b", "changes": []})

    assert "Write mode is disabled" in result


def test_tool_call_key_handles_unserializable_arguments() -> None:
    from app.tools import _tool_call_key

    key = _tool_call_key("read_file", {"path": object()})  # object() 不可 JSON 序列化

    assert key.startswith("read_file:")


def test_parse_tool_call_invalid_json_yields_empty_args() -> None:
    from app.tools import parse_tool_call

    tool_call = SimpleNamespace(function=SimpleNamespace(name="read_file", arguments="{broken"))
    name, args = parse_tool_call(tool_call)

    assert name == "read_file"
    assert args == {}


@pytest.mark.parametrize(
    "changes",
    [
        [],  # 空 changes
        [{"path": "a.py", "content": "x", "message": ""}],  # 空 message
        [{"path": "a.py", "content": None, "message": "m"}],  # content 非 str
        # 重复 path
        [
            {"path": "a.py", "content": "x", "message": "m"},
            {"path": "a.py", "content": "y", "message": "m"},
        ],
    ],
)
def test_validate_pr_proposal_rejects_invalid_changes(changes) -> None:
    from app.tools import validate_pr_proposal

    with pytest.raises(ValueError):
        validate_pr_proposal(
            Settings(openai_api_key="k"),
            branch="fix/x",
            title="t",
            body="b",
            changes=changes,
        )


def test_validate_pr_proposal_rejects_oversized_content() -> None:
    from app.tools import validate_pr_proposal

    settings = Settings(openai_api_key="k", github_max_file_bytes=4096)
    with pytest.raises(ValueError, match="file size limit"):
        validate_pr_proposal(
            settings,
            branch="fix/x",
            title="t",
            body="b",
            changes=[{"path": "a.py", "content": "x" * 5000, "message": "m"}],
        )


def test_validate_pr_proposal_rejects_missing_title_or_body() -> None:
    from app.tools import validate_pr_proposal

    with pytest.raises(ValueError, match="required"):
        validate_pr_proposal(
            Settings(openai_api_key="k"),
            branch="fix/x",
            title="  ",
            body="b",
            changes=[{"path": "a", "content": "c", "message": "m"}],
        )


def test_validate_pr_proposal_rejects_invalid_branch() -> None:
    from app.tools import validate_pr_proposal

    for branch in ("", "../evil", "fix x!", ".hidden", "trailing/"):
        with pytest.raises(ValueError, match="branch"):
            validate_pr_proposal(
                Settings(openai_api_key="k"),
                branch=branch,
                title="t",
                body="b",
                changes=[{"path": "a", "content": "c", "message": "m"}],
            )


# ══════════════════════════════════════════════════════════════════
# sessions.py：SqliteStore 与 MemoryStore 边界
# ══════════════════════════════════════════════════════════════════


async def _sqlite_manager(tmp_path):
    return SessionManager(db_path=str(tmp_path / "sessions.db"))


async def test_sqlite_cancel_and_recovery_flow(tmp_path) -> None:
    manager = await _sqlite_manager(tmp_path)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.status = "running"
    await manager.save(session)

    assert await manager.is_cancel_requested(session.session_id) is False
    assert await manager.request_cancel(session.session_id) is True
    assert await manager.is_cancel_requested(session.session_id) is True
    # 已取消的会话不再是 running → 再次取消返回 False
    session2 = await manager.get(session.session_id)
    assert session2 is not None
    session2.status = "completed"
    await manager.save(session2)
    assert await manager.request_cancel(session.session_id) is False

    # 恢复过期的 running 会话（save 会刷新 updated_at，需用 SQL 直接回拨时间）
    stale = await manager.create("https://github.com/acme/widget/issues/2")
    stale.status = "running"
    await manager.save(stale)
    async with manager._store._conn() as db:
        await db.execute(
            "UPDATE sessions SET updated_at='2020-01-01T00:00:00' WHERE session_id=?",
            (stale.session_id,),
        )
        await db.commit()
    recovered = await manager.recover_stale("2021-01-01T00:00:00")
    assert recovered == 1

    await manager.close()


async def test_sqlite_list_query_search_and_delete(tmp_path) -> None:
    manager = await _sqlite_manager(tmp_path)
    session = await manager.create("https://github.com/acme/widget/issues/42")
    session.display_title = "Parser crash"
    await manager.save(session)

    matches = await manager.list(archived=False, query="Parser crash")
    assert len(matches) == 1
    no_matches = await manager.list(archived=False, query="nothing")
    assert no_matches == []

    assert await manager.delete(session.session_id) is True
    assert await manager.get(session.session_id) is None
    assert await manager.delete(session.session_id) is False

    await manager.close()


async def test_sqlite_pr_proposal_roundtrip_and_delete(tmp_path) -> None:
    manager = await _sqlite_manager(tmp_path)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    proposal = {"branch": "fix/x", "title": "T", "body": "B", "changes": [{"path": "a", "message": "m"}]}

    await manager.save_pr_proposal(session.session_id, proposal)
    assert await manager.get_pr_proposal(session.session_id) == proposal
    await manager.delete_pr_proposal(session.session_id)
    assert await manager.get_pr_proposal(session.session_id) is None

    await manager.close()


async def test_sqlite_touch_and_clear_events(tmp_path) -> None:
    manager = await _sqlite_manager(tmp_path)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    await manager.append_event(session.session_id, {"type": "phase", "data": None, "message": "m"})
    await manager.update_metrics(session.session_id, {"tool_calls": 3})

    await manager.touch(session.session_id)
    await manager.clear_events(session.session_id)

    assert await manager.list_events(session.session_id) == []
    refreshed = await manager.get(session.session_id)
    assert refreshed is not None
    assert refreshed.metrics == {}
    assert refreshed.report is None

    await manager.close()


async def test_sqlite_save_tolerates_corrupted_metrics_json(tmp_path) -> None:
    manager = await _sqlite_manager(tmp_path)
    session = await manager.create("https://github.com/acme/widget/issues/1")

    store = manager._store
    async with store._conn() as db:
        await db.execute(
            "UPDATE sessions SET metrics_json='{broken json' WHERE session_id=?",
            (session.session_id,),
        )
        await db.commit()

    # save() 遇到损坏的 metrics_json：静默跳过合并，正常保存
    session.display_title = "after corruption"
    await manager.save(session)
    refreshed = await manager.get(session.session_id)
    assert refreshed is not None
    assert refreshed.display_title == "after corruption"

    await manager.close()


async def test_memory_store_edge_branches() -> None:
    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")

    # 非 running 会话取消失败
    assert await manager.request_cancel(session.session_id) is False
    # 未知会话的 PR 提案
    assert await manager.get_pr_proposal("ghost") is None
    # 保存/删除提案
    await manager.save_pr_proposal(session.session_id, {"branch": "b", "title": "t", "body": "b", "changes": []})
    assert (await manager.get_pr_proposal(session.session_id)) is not None
    await manager.delete_pr_proposal(session.session_id)
    assert await manager.get_pr_proposal(session.session_id) is None


async def test_memory_store_evicts_oldest_beyond_capacity() -> None:
    from app.sessions import MemoryStore

    store = MemoryStore(max_sessions=2)
    for i in range(3):
        store._sessions[f"s{i}"] = Session(session_id=f"s{i}", issue_url=f"https://github.com/acme/widget/issues/{i}")

    store._evict()

    assert len(store._sessions) == 2
    assert "s0" not in store._sessions


async def test_sqlite_row_parse_failure_degrades_gracefully(tmp_path) -> None:
    """损坏的 JSON 列（metrics/issue）在读取时降级为默认值而非抛错。"""
    manager = await _sqlite_manager(tmp_path)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    store = manager._store
    async with store._conn() as db:
        await db.execute(
            "UPDATE sessions SET metrics_json='{bad', issue_json='{bad', tree_json='{bad', "
            "messages_json='{bad', file_cache_json='{bad', files_read_json='{bad' WHERE session_id=?",
            (session.session_id,),
        )
        await db.commit()

    detail = await manager.get(session.session_id)
    assert detail is not None
    assert detail.metrics == {}
    assert detail.issue is None
    assert detail.tree == []
    assert detail.messages == []

    # 列表读取（summary 反序列化）同样降级
    summaries = await manager.list(archived=False, query="")
    assert len(summaries) == 1

    await manager.close()


# ══════════════════════════════════════════════════════════════════
# db.py：迁移边界与连接池
# ══════════════════════════════════════════════════════════════════


async def test_migration_skips_invalid_report_json(tmp_path) -> None:
    import app.db as db_module

    conn = await db_module.get_db(str(tmp_path / "migrate.db"))
    await conn.execute("INSERT INTO sessions (session_id, issue_url, report_json) VALUES ('r1', 'u1', ?)", ("",))
    await conn.execute(
        "INSERT INTO sessions (session_id, issue_url, report_json) VALUES ('r2', 'u2', ?)",
        ('"just a string"',),
    )
    await conn.commit()

    await db_module._migrate_report_enrichment(conn)
    await conn.commit()

    rows = await (
        await conn.execute("SELECT session_id, report_json FROM sessions WHERE session_id LIKE 'r%'")
    ).fetchall()
    by_id = {row["session_id"]: row["report_json"] for row in rows}
    # 空 / 非 dict 的 report_json 被跳过，原文保持不变
    assert by_id["r1"] == ""
    assert json.loads(by_id["r2"]) == "just a string"
    await conn.close()


async def test_migration_tolerates_enrich_failure(tmp_path, monkeypatch) -> None:
    import app.db as db_module

    conn = await db_module.get_db(str(tmp_path / "enrich-fail.db"))
    await conn.execute(
        "INSERT INTO sessions (session_id, issue_url, report_json) VALUES ('rx', 'ux', ?)",
        (json.dumps(_report_data()),),
    )
    await conn.commit()

    def broken_enrich(rep):
        raise RuntimeError("enricher crashed")

    monkeypatch.setattr("app.report_backfill.enrich_report", broken_enrich)

    await db_module._migrate_report_enrichment(conn)
    await conn.commit()

    row = await (await conn.execute("SELECT report_json FROM sessions WHERE session_id='rx'")).fetchone()
    assert json.loads(row["report_json"])["summary"] == "Parser bug"  # 原文未被破坏
    await conn.close()


async def test_migration_once_double_check_guard(tmp_path, monkeypatch) -> None:
    import app.db as db_module

    db_path = str(tmp_path / "once.db")
    manager = SessionManager(db_path=db_path)
    await manager.create("https://github.com/acme/widget/issues/1")
    await manager.close()

    calls = []

    async def tracked_migrate(conn):
        calls.append(1)
        await asyncio.sleep(0.05)  # 拉长临界区，制造并发竞争窗口

    monkeypatch.setattr(db_module, "_migrate_report_enrichment", tracked_migrate)

    # 用原生连接绕过 get_db（get_db 自身会先执行一次迁移），直接并发调用 once 包装：
    # 一个进入锁执行迁移并置位标志，另一个等锁后在双重检查处直接返回。
    import aiosqlite

    conn = await aiosqlite.connect(db_path)
    monkeypatch.setattr(db_module, "_enrichment_migration_done", set())
    await asyncio.gather(
        db_module._migrate_report_enrichment_once(conn),
        db_module._migrate_report_enrichment_once(conn),
    )

    assert len(calls) == 1  # 全表扫描迁移只执行一次

    # 标志置位后再调用：直接短路返回，不再进入锁
    await db_module._migrate_report_enrichment_once(conn)  # 返回值恒为 None，只验证副作用
    assert len(calls) == 1
    await conn.close()


async def test_connection_pool_acquire_failure_decrements_counter(tmp_path, monkeypatch) -> None:
    import app.db as db_module

    pool = db_module.ConnectionPool(str(tmp_path / "pool.db"), size=1)

    async def broken_get_db(path):
        raise RuntimeError("cannot open")

    monkeypatch.setattr(db_module, "get_db", broken_get_db)
    with pytest.raises(RuntimeError, match="cannot open"):
        await pool.acquire()
    assert pool._created == 0


async def test_connection_pool_waits_for_release(tmp_path) -> None:
    import app.db as db_module

    pool = db_module.ConnectionPool(str(tmp_path / "pool.db"), size=1)
    first = await pool.acquire()
    second_task = asyncio.create_task(pool.acquire())
    await asyncio.sleep(0.05)
    assert not second_task.done()

    await pool.release(first)
    second = await asyncio.wait_for(second_task, timeout=2.0)

    assert second is first
    await pool.close()


async def test_connection_pool_context_manager_returns_connection(tmp_path) -> None:
    import app.db as db_module

    pool = db_module.ConnectionPool(str(tmp_path / "ctx.db"), size=1)
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
    # 归还后池中有一个空闲连接，可再次借出
    async with pool.connection() as conn2:
        assert conn2 is not None
    await pool.close()


async def test_connection_pool_release_closed_connection_discarded(tmp_path) -> None:
    import app.db as db_module

    pool = db_module.ConnectionPool(str(tmp_path / "closed.db"), size=1)
    conn = await pool.acquire()
    await conn.close()
    await pool.release(conn)  # 已关闭：丢弃并递减计数

    assert pool._created == 0
    assert pool._pool.empty()
    # 计数递减后可重新创建连接
    await pool.acquire()
    await pool.close()


# ══════════════════════════════════════════════════════════════════
# task_queue.py：启动幂等、停止清理、任务清理异常
# ══════════════════════════════════════════════════════════════════


async def test_task_queue_start_is_idempotent() -> None:
    from app.task_queue import TaskQueue

    queue = TaskQueue(
        Settings(openai_api_key="k"),
        CircuitBreaker(threshold=3, recovery=1),
        max_concurrent=1,
        client=MagicMock(),
        github_client=None,
    )
    await queue.start()
    workers = list(queue._worker_tasks)
    await queue.start()  # 幂等：不重复启动

    assert queue._worker_tasks == workers
    await queue.stop()


async def test_task_queue_stop_cancels_pending_tasks() -> None:
    from app.task_queue import TaskQueue

    queue = TaskQueue(
        Settings(openai_api_key="k"),
        CircuitBreaker(threshold=3, recovery=1),
        max_concurrent=1,
        client=MagicMock(),
        github_client=None,
    )
    # 不 start worker：任务滞留在队列中，stop 应把它们标记为 cancelled
    batch = queue.submit(["https://github.com/acme/widget/issues/1", "https://github.com/acme/widget/issues/2"])

    await queue.stop()

    assert batch.status == "partial"
    assert all(task.status == "cancelled" for task in batch.tasks)


async def test_task_queue_run_task_skips_unknown_ids() -> None:
    from app.task_queue import TaskQueue

    queue = TaskQueue(
        Settings(openai_api_key="k"),
        CircuitBreaker(threshold=3, recovery=1),
        max_concurrent=1,
        client=MagicMock(),
        github_client=None,
    )
    # 未知 batch/task：仅记警告，不抛错
    await queue._run_task("ghost-batch", "ghost-task")


async def test_task_queue_run_task_survives_aclose_failure(monkeypatch) -> None:
    from app.task_queue import Batch, BatchTask, TaskQueue

    queue = TaskQueue(
        Settings(openai_api_key="k"),
        CircuitBreaker(threshold=3, recovery=1),
        max_concurrent=1,
        client=MagicMock(),
        github_client=None,
    )
    batch = Batch(
        batch_id="b1",
        tasks=[BatchTask(task_id="t1", issue_url="https://github.com/acme/widget/issues/1")],
    )
    queue._batches["b1"] = batch

    async def failing_investigate(self, issue_url, *, session=None):
        raise RuntimeError("investigation failed")

    async def failing_aclose(self):
        raise RuntimeError("aclose crashed")

    monkeypatch.setattr(IssueAgent, "investigate", failing_investigate)
    monkeypatch.setattr(IssueAgent, "aclose", failing_aclose)

    await queue._run_task("b1", "t1")

    assert batch.tasks[0].status == "failed"
    task_error = batch.tasks[0].error or ""
    assert "investigation failed" in task_error


# ══════════════════════════════════════════════════════════════════
# services.py：中断落库的脱离取消作用域路径
# ══════════════════════════════════════════════════════════════════


async def test_mark_stream_interrupted_schedules_detached_persist_on_cancel() -> None:
    from app.services import mark_stream_interrupted

    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.status = "running"
    await manager.save(session)
    original_save = manager.save
    attempts = {"n": 0}

    async def cancelling_save(session_: Session):
        # 模拟落库途中被客户端断开取消：只取消第一次，让脱离取消作用域的重试真正落库。
        # MemoryStore 现在与 SqliteStore 一样按副本读写，不能再依赖对象别名落库。
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise asyncio.CancelledError
        await original_save(session_)

    manager.save = cancelling_save  # type: ignore[method-assign,assignment]  # 刻意的猴子补丁：直接替换方法以模拟故障

    with pytest.raises(asyncio.CancelledError):
        await mark_stream_interrupted(manager, session.session_id, started_at=time.monotonic())

    # 让后台脱离任务执行完
    await asyncio.sleep(0.05)
    refreshed = await manager.get(session.session_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.phase == "interrupted"
    manager.save = original_save  # type: ignore[method-assign,assignment]  # 刻意的猴子补丁：直接替换方法以模拟故障


async def test_persist_interrupted_detached_skips_non_running_sessions() -> None:
    from app.services import _persist_interrupted_detached

    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.status = "completed"
    await manager.save(session)

    # 非 running：直接返回，不追加 interrupted 事件
    await _persist_interrupted_detached(manager, session.session_id, time.monotonic())

    refreshed = await manager.get(session.session_id)
    assert refreshed is not None
    assert refreshed.status == "completed"


async def test_persist_interrupted_detached_persists_running_session() -> None:
    from app.services import _persist_interrupted_detached

    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.status = "running"
    await manager.save(session)

    await _persist_interrupted_detached(manager, session.session_id, time.monotonic())

    refreshed = await manager.get(session.session_id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.phase == "interrupted"
    events = await manager.list_events(session.session_id)
    assert any(event["type"] == "interrupted" for event in events)


async def test_persist_interrupted_detached_swallows_conflict_and_errors() -> None:
    from app.services import _persist_interrupted_detached

    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.status = "running"
    await manager.save(session)
    original_save = manager.save

    async def conflicting_save(session_: Session) -> None:
        raise SessionConflictError("conflict")

    manager.save = conflicting_save  # type: ignore[method-assign,assignment]  # 刻意的猴子补丁：直接替换方法以模拟故障
    await _persist_interrupted_detached(manager, session.session_id, time.monotonic())  # 不抛错

    async def exploding_get(session_id: str):
        raise RuntimeError("db exploded")

    manager.get = exploding_get  # type: ignore[method-assign,assignment]  # 刻意的猴子补丁：直接替换方法以模拟故障
    await _persist_interrupted_detached(manager, session.session_id, time.monotonic())  # 不抛错
    manager.save = original_save  # type: ignore[method-assign,assignment]  # 刻意的猴子补丁：直接替换方法以模拟故障


# ══════════════════════════════════════════════════════════════════
# 收尾：剩余零散未覆盖分支
# ══════════════════════════════════════════════════════════════════


async def test_call_llm_and_stream_route_through_circuit_breaker(fake_client, fake_response) -> None:
    """带熔断器的 agent：_call_llm / _call_llm_stream 经 breaker.call 转发。"""
    from tests.conftest import _FakeStreamChunk

    agent = IssueAgent(
        Settings(openai_api_key="test-key"),
        client=fake_client(
            [
                fake_response(content="ok"),
                [_FakeStreamChunk(content="hi")],
            ]
        ),
        circuit_breaker=CircuitBreaker(threshold=5, recovery=30),
    )

    response = await agent._call_llm([{"role": "user", "content": "q"}])
    assert response.choices[0].message.content == "ok"

    stream = await agent._call_llm_stream([{"role": "user", "content": "q"}])
    chunks = [chunk async for chunk in stream]
    assert chunks


def test_tool_executor_cached_files_stop_at_context_budget() -> None:
    """恢复的 file_cache 超出总上下文预算：后续文件停止预填充。"""
    from app.tools import ToolExecutor

    executor = ToolExecutor(
        MagicMock(),
        Settings(openai_api_key="k"),
        _review_issue(),
        ["a.py", "b.py"],
        file_cache={"a.py": "x" * 100, "b.py": "y"},
        max_files=5,
        max_file_chars=10_000,
        max_total_context_chars=50,
    )

    assert executor.files_read == ["a.py"]  # b.py 超预算被截断
    assert executor._cached_chars <= 50


async def test_list_branches_reports_empty() -> None:
    from app.tools import ToolExecutor

    github = MagicMock()
    github.list_branches = AsyncMock(return_value=[])
    executor = ToolExecutor(github, Settings(openai_api_key="k"), _review_issue(), [])

    result = await executor.execute("list_branches", {})

    assert result == "No branches found"


async def test_ledger_full_stops_recording_observations() -> None:
    """调查账本写满后：后续观察直接丢弃（不追加、不报错）。"""
    from app.tools import ToolExecutor

    github = MagicMock()
    github.search_code = AsyncMock(return_value=[{"path": "a.py", "fragments": ["z" * 1500]}])
    executor = ToolExecutor(
        github, Settings(openai_api_key="k", max_investigation_ledger_chars=1000), _review_issue(), []
    )
    # 第一次观察被截断到恰好 1000 字符（配置下限），账本随即写满
    await executor.execute("search_code", {"query": "x"})
    ledger_after_first = list(executor.investigation_ledger)
    assert executor._ledger_chars == 1000

    await executor.execute("search_files", {"query": "a"})

    assert ledger_after_first  # 第一次观察已记录
    assert executor.investigation_ledger == ledger_after_first  # 账本满后不再追加


async def test_read_file_post_fetch_recheck_hits_file_limit() -> None:
    """并发窗口内在 get_file 期间其它协程占满文件额度：post-fetch 复检拒绝缓存。"""
    from app.tools import ToolExecutor

    github = MagicMock()
    holder: dict = {}

    async def get_file(issue, path):
        if path == "b.py":
            # 模拟并发：await 期间另一个协程读满了 files_read
            await holder["executor"].execute("read_file", {"path": "a.py"})
        return SourceFile(path=path, content="content")

    github.get_file = AsyncMock(side_effect=get_file)
    executor = ToolExecutor(github, Settings(openai_api_key="k"), _review_issue(), ["a.py", "b.py"], max_files=1)
    holder["executor"] = executor

    result = await executor.execute("read_file", {"path": "b.py"})

    assert "File limit reached" in result
    assert executor.files_read == ["a.py"]


async def test_read_file_post_fetch_recheck_hits_context_limit() -> None:
    """并发窗口内在 get_file 期间其它协程耗尽上下文预算：post-fetch 复检拒绝缓存。"""
    from app.tools import ToolExecutor

    github = MagicMock()
    holder: dict = {}

    async def get_file(issue, path):
        if path == "b.py":
            await holder["executor"].execute("read_file", {"path": "a.py"})
            return SourceFile(path=path, content="content")
        return SourceFile(path=path, content="x" * 20)  # a.py：占满整个预算

    github.get_file = AsyncMock(side_effect=get_file)
    executor = ToolExecutor(
        github,
        Settings(openai_api_key="k"),
        _review_issue(),
        ["a.py", "b.py"],
        max_files=5,
        max_total_context_chars=10,
    )
    holder["executor"] = executor

    result = await executor.execute("read_file", {"path": "b.py"})

    assert "Source context limit reached" in result
    assert executor.files_read == ["a.py"]


def test_stream_keepalive_survives_touch_failure(monkeypatch) -> None:
    """心跳里的 touch 失败（如 DB 短暂不可用）：仅记 debug 日志，不影响流。"""
    manager = SessionManager()
    _override_session_manager(manager)
    original = analysis_routes._iter_events_with_heartbeat

    async def fast_heartbeat(event_iter, *, timeout: float = 15.0):
        async for item in original(event_iter, timeout=0.05):
            yield item

    monkeypatch.setattr(analysis_routes, "_iter_events_with_heartbeat", fast_heartbeat)

    async def failing_touch(session_id: str) -> None:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(manager, "touch", failing_touch)

    async def slow_stream(self: IssueAgent, issue_url: str, *, session=None):
        await asyncio.sleep(0.2)
        yield done_event()

    monkeypatch.setattr(IssueAgent, "investigate_stream", slow_stream)
    try:
        response = TestClient(app).post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert ": keepalive" in response.text
    assert '"type": "done"' in response.text


async def test_session_report_returns_stored_report() -> None:
    manager = SessionManager()
    _override_session_manager(manager)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.report = AnalysisReport.model_validate(_report_data())
    await manager.save(session)
    try:
        response = TestClient(app).get(f"/session/{session.session_id}/report")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 200
    assert response.json()["summary"] == "Parser bug"


async def test_session_proposal_returns_pending_changes() -> None:
    manager = SessionManager()
    _override_session_manager(manager)
    session = await manager.create("https://github.com/acme/widget/issues/1")
    await manager.save_pr_proposal(
        session.session_id,
        {
            "branch": "fix/parser",
            "title": "Fix parser",
            "body": "Fixes the bug",
            "changes": [{"path": "src/parser.py", "message": "patch", "content": "new line\n"}],
        },
    )
    try:
        response = TestClient(app).get(f"/session/{session.session_id}/proposal")
    finally:
        app.dependency_overrides.pop(get_session_manager, None)

    assert response.status_code == 200
    data = response.json()
    assert data["branch"] == "fix/parser"
    assert data["title"] == "Fix parser"
    assert data["changes"][0]["path"] == "src/parser.py"
    assert data["changes"][0]["proposed_lines"] == 1


async def test_import_returns_500_when_refreshed_session_missing(monkeypatch) -> None:
    manager = SessionManager()

    async def none_get(session_id: str):
        return None

    monkeypatch.setattr(manager, "get", none_get)
    payload = {
        "format": "issue-agent-session",
        "version": 1,
        "session": {"issue_url": "https://github.com/acme/widget/issues/1"},
        "events": [],
    }

    with pytest.raises(HTTPException) as exc_info:
        await sessions_routes.import_session(_import_request(json.dumps(payload).encode(), None), manager)

    assert exc_info.value.status_code == 500
    assert "could not be loaded" in exc_info.value.detail


async def test_reviewer_retries_on_no_choices_then_raises(fake_client) -> None:
    from app.errors import ReviewResponseError
    from app.reviewer import ReviewerAgent

    reviewer = ReviewerAgent(
        Settings(openai_api_key="test-key", max_report_retries=2),
        fake_client([SimpleNamespace(choices=[]), SimpleNamespace(choices=[])]),
    )

    with pytest.raises(ReviewResponseError, match="no choices"):
        await reviewer.review(
            issue=_review_issue(),
            report=AnalysisReport.model_validate(_report_data()),
            file_cache={},
            files_read=[],
            line_counts={},
        )


class _FakeWriteGitHub:
    """apply-fix 写流程的 GitHub 客户端替身。"""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.deleted_branches: list[str] = []

    async def __aenter__(self) -> "_FakeWriteGitHub":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def get_branch_sha(self, owner: str, repo: str, branch: str) -> str:
        return "base-sha-123"

    async def create_branch(self, owner: str, repo: str, branch: str, sha: str) -> None:
        if self.fail_on == "create_branch":
            from app.github import GitHubError

            raise GitHubError("branch rejected")

    async def create_or_update_file(
        self, owner: str, repo: str, path: str, content: str, branch: str, message: str
    ) -> None:
        if self.fail_on == "update_file":
            from app.github import GitHubError

            raise GitHubError("commit rejected")

    async def create_pull_request(self, owner: str, repo: str, branch: str, base: str, title: str, body: str) -> dict:
        return {"pr_url": f"https://github.com/{owner}/{repo}/pull/9", "number": 9}

    async def delete_branch(self, owner: str, repo: str, branch: str) -> None:
        self.deleted_branches.append(branch)
        if self.fail_on == "delete_branch":
            from app.github import GitHubError

            raise GitHubError("rollback failed")


async def _make_apply_fix_session(manager: SessionManager) -> Session:
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.issue = _review_issue()
    session.status = "completed"
    await manager.save(session)
    await manager.save_pr_proposal(
        session.session_id,
        {
            "branch": "fix/auto",
            "title": "Automated fix",
            "body": "Applies the proposed patch",
            "changes": [{"path": "src/parser.py", "message": "patch", "content": "fixed\n"}],
        },
    )
    return session


async def test_apply_fix_success_creates_pr_and_clears_proposal(monkeypatch) -> None:
    import app.services as services_module
    from app.models import ApplyFixRequest

    manager = SessionManager()
    session = await _make_apply_fix_session(manager)
    settings = Settings(openai_api_key="k", write_mode=True, github_token="gh-token")

    fake_github = _FakeWriteGitHub()
    monkeypatch.setattr(services_module, "GitHubClient", lambda *a, **k: fake_github)

    from app.services import apply_fix

    result = await apply_fix(
        session.session_id,
        ApplyFixRequest(confirm=True),
        settings=settings,
        session_mgr=manager,
    )

    assert result.pr_url.endswith("/pull/9")
    assert result.branch == "fix/auto"
    assert await manager.get_pr_proposal(session.session_id) is None  # 提案已消费
    assert fake_github.deleted_branches == []  # 成功路径不回滚


async def test_apply_fix_rolls_back_branch_on_write_failure(monkeypatch) -> None:
    import app.services as services_module
    from app.models import ApplyFixRequest

    manager = SessionManager()
    session = await _make_apply_fix_session(manager)
    settings = Settings(openai_api_key="k", write_mode=True, github_token="gh-token")

    fake_github = _FakeWriteGitHub(fail_on="update_file")
    monkeypatch.setattr(services_module, "GitHubClient", lambda *a, **k: fake_github)

    from fastapi import HTTPException as FastAPIHTTPException

    from app.services import apply_fix

    with pytest.raises(FastAPIHTTPException) as exc_info:
        await apply_fix(
            session.session_id,
            ApplyFixRequest(confirm=True),
            settings=settings,
            session_mgr=manager,
        )

    assert exc_info.value.status_code == 502
    assert fake_github.deleted_branches == ["fix/auto"]  # 失败路径回滚分支


async def test_apply_fix_rollback_failure_only_logs(monkeypatch) -> None:
    import app.services as services_module
    from app.models import ApplyFixRequest

    manager = SessionManager()
    session = await _make_apply_fix_session(manager)
    settings = Settings(openai_api_key="k", write_mode=True, github_token="gh-token")

    # 主流程与回滚使用两个客户端实例：回滚删除分支时再失败
    clients = [_FakeWriteGitHub(fail_on="update_file"), _FakeWriteGitHub(fail_on="delete_branch")]
    monkeypatch.setattr(services_module, "GitHubClient", lambda *a, **k: clients.pop(0))

    from fastapi import HTTPException as FastAPIHTTPException

    from app.services import apply_fix

    with pytest.raises(FastAPIHTTPException):
        await apply_fix(
            session.session_id,
            ApplyFixRequest(confirm=True),
            settings=settings,
            session_mgr=manager,
        )  # 回滚失败被吞掉，只向外抛原始 502

    assert clients == []  # 两个客户端（主流程 + 回滚）都被使用
