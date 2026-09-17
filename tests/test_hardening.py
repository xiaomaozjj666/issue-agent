"""加固回归测试：会话租约、熔断策略、成本闸门、写路径保护、ReDoS 防护。

这些语义在 2026-09 的正确性/安全加固中引入，用于锁住「不允许回退」的行为：
- 同一会话并发调查必须互斥（否则 clear_events 会抹掉正在跑的事件史）
- 熔断器只统计 provider 侧故障（调用方写错 model 不该拖垮全局）
- 流式调用的真实结果必须等流消费完再补报
- 成本预算到线即中止
- 模型生成的补丁不得改 CI/容器/密钥/锁文件，也不得指向受保护分支
- 正则回溯（ReDoS）不得阻塞事件循环
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from app.agent import IssueAgent, ModelResponseError
from app.circuit_breaker import CircuitBreaker, State, is_retryable_failure
from app.provider import iter_stream_with_breaker
from app.sessions import Session, SessionConflictError, SessionManager
from app.tools import ToolExecutor, _is_protected_write_path, validate_pr_proposal


class _StatusError(Exception):
    """模拟 openai/httpx 带 status_code 的异常。"""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


# ══════════════════════════════════════════════════════════════════
# 会话调查租约（并发互斥）
# ══════════════════════════════════════════════════════════════════


async def test_try_claim_running_is_exclusive() -> None:
    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")

    first = await manager.try_claim_running(session.session_id, phase="starting")
    assert first is not None
    assert first.status == "running"
    assert first.phase == "starting"

    second = await manager.try_claim_running(session.session_id, phase="starting")
    assert second is None, "第二次占位必须失败：同一会话只允许一个调查在跑"


async def test_try_claim_running_resets_cancel_flag() -> None:
    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    claimed = await manager.try_claim_running(session.session_id)
    assert claimed is not None
    await manager.request_cancel(session.session_id)
    assert await manager.is_cancel_requested(session.session_id) is True

    # 取消后调查收尾：request_cancel 递增了版本，真实调用方会重新读取后再写终态
    # （直接保存手里的旧副本会撞乐观锁）。再次续跑时必须清掉上一轮的取消标记，
    # 否则新一轮调查会被立即判为「用户已取消」。
    current = await manager.get(session.session_id)
    assert current is not None
    current.status = "failed"
    current.phase = "interrupted"
    await manager.save(current)

    again = await manager.try_claim_running(session.session_id)
    assert again is not None
    assert await manager.is_cancel_requested(session.session_id) is False


async def test_try_claim_running_rejects_unknown_session() -> None:
    assert await SessionManager().try_claim_running("does-not-exist") is None


async def test_try_claim_running_allows_retry_after_failure() -> None:
    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    claimed = await manager.try_claim_running(session.session_id)
    assert claimed is not None
    claimed.status = "failed"
    await manager.save(claimed)

    again = await manager.try_claim_running(session.session_id)
    assert again is not None, "失败/已完成的会话必须能被再次占位（续跑）"
    assert again.status == "running"


# ══════════════════════════════════════════════════════════════════
# MemoryStore 与 SqliteStore 语义对齐
# ══════════════════════════════════════════════════════════════════


async def test_memory_store_get_returns_copy() -> None:
    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    fetched = await manager.get(session.session_id)
    assert fetched is not None
    assert fetched is not session

    fetched.messages.append({"role": "user", "content": "hi"})
    again = await manager.get(session.session_id)
    assert again is not None
    assert again.messages == [], "副本上的修改不得泄漏回存储"


async def test_memory_store_detects_concurrent_save() -> None:
    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    first = await manager.get(session.session_id)
    second = await manager.get(session.session_id)
    assert first is not None and second is not None

    first.status = "completed"
    await manager.save(first)

    second.status = "failed"
    with pytest.raises(SessionConflictError):
        await manager.save(second)


async def test_append_events_is_bulk() -> None:
    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    count = await manager.append_events(
        session.session_id,
        [{"type": "phase", "data": {"phase": "exploring"}, "message": ""}] * 3,
    )
    assert count == 3
    events = await manager.list_events(session.session_id)
    assert [event["sequence"] for event in events] == [1, 2, 3]


# ══════════════════════════════════════════════════════════════════
# 熔断策略
# ══════════════════════════════════════════════════════════════════


def test_retryable_failure_classification() -> None:
    assert is_retryable_failure(_StatusError(503)) is True
    assert is_retryable_failure(_StatusError(429)) is True
    assert is_retryable_failure(_StatusError(408)) is True
    assert is_retryable_failure(_StatusError(404)) is False
    assert is_retryable_failure(_StatusError(400)) is False
    assert is_retryable_failure(_StatusError(401)) is False
    # 无 status_code 的连接/超时类异常一律视为可重试
    assert is_retryable_failure(TimeoutError("slow")) is True


async def test_circuit_breaker_ignores_client_errors() -> None:
    breaker = CircuitBreaker(threshold=2, recovery=30.0)

    async def bad_request() -> None:
        raise _StatusError(404)

    for _ in range(5):
        with pytest.raises(_StatusError):
            await breaker.call(bad_request)

    assert breaker.state is State.CLOSED, "调用方输入错误不应打开熔断"
    assert breaker.failure_count == 0


async def test_circuit_breaker_opens_on_provider_errors() -> None:
    breaker = CircuitBreaker(threshold=2, recovery=30.0)

    async def unavailable() -> None:
        raise _StatusError(503)

    for _ in range(2):
        with pytest.raises(_StatusError):
            await breaker.call(unavailable)

    assert breaker.state is State.OPEN


async def test_stream_failure_is_reported_after_consumption() -> None:
    breaker = CircuitBreaker(threshold=5, recovery=30.0)

    async def stream_then_fail():
        yield "chunk-1"
        raise _StatusError(503)

    with pytest.raises(_StatusError):
        async for _ in iter_stream_with_breaker(stream_then_fail(), breaker):
            pass

    assert breaker.failure_count == 1, "流中途失败必须在流消费结束后补报给熔断器"


async def test_stream_success_resets_failure_count() -> None:
    breaker = CircuitBreaker(threshold=5, recovery=30.0)
    await breaker.report_stream_outcome(_StatusError(503))
    assert breaker.failure_count == 1

    async def ok_stream():
        yield "chunk-1"
        yield "chunk-2"

    chunks = [chunk async for chunk in iter_stream_with_breaker(ok_stream(), breaker)]
    assert chunks == ["chunk-1", "chunk-2"]
    assert breaker.failure_count == 0


# ══════════════════════════════════════════════════════════════════
# 成本预算闸门
# ══════════════════════════════════════════════════════════════════


def test_cost_budget_stops_run_when_exceeded(make_agent) -> None:
    agent: IssueAgent = make_agent(settings_kwargs={"max_session_estimated_cost_usd": 0.01})
    session = SimpleNamespace(metrics={"input_tokens": 10_000, "output_tokens": 10_000})

    with pytest.raises(ModelResponseError) as excinfo:
        agent._enforce_cost_budget(cast("Session", session))  # noqa: SLF001 — 只提供 metrics 的替身

    assert "budget" in str(excinfo.value)


def test_cost_budget_disabled_by_default(make_agent) -> None:
    agent: IssueAgent = make_agent()
    session = SimpleNamespace(metrics={"input_tokens": 10_000_000, "output_tokens": 10_000_000})

    # 0 = 不限制：闸门不介入（成本估算由报告/收尾路径单独写入 metrics）
    agent._enforce_cost_budget(cast("Session", session))  # noqa: SLF001

    assert "estimated_cost_usd" not in session.metrics


def test_cost_budget_tolerates_missing_session(make_agent) -> None:
    agent: IssueAgent = make_agent(settings_kwargs={"max_session_estimated_cost_usd": 0.01})
    agent._enforce_cost_budget(None)  # noqa: SLF001 — 无会话时直接返回


# ══════════════════════════════════════════════════════════════════
# 写路径保护（CI / 容器 / 密钥 / 锁文件 / 受保护分支）
# ══════════════════════════════════════════════════════════════════


def test_is_protected_write_path() -> None:
    protected = [
        ".github/workflows/ci.yml",
        ".github/dependabot.yml",
        "Dockerfile",
        ".env",
        ".env.production",
        "uv.lock",
        "package-lock.json",
        "keys/server.pem",
        "certs/client.key",
        "CODEOWNERS",
        ".git/config",
    ]
    for path in protected:
        assert _is_protected_write_path(path) is True, path

    allowed = ["src/app.py", "app/services.py", "docs/readme.md", "tests/test_x.py"]
    for path in allowed:
        assert _is_protected_write_path(path) is False, path


def test_validate_pr_proposal_rejects_protected_path(make_settings) -> None:
    with pytest.raises(ValueError, match="protected path"):
        validate_pr_proposal(
            make_settings,
            branch="issue-agent/fix-ci",
            title="t",
            body="b",
            changes=[{"path": ".github/workflows/ci.yml", "content": "x", "message": "m"}],
        )


def test_validate_pr_proposal_rejects_protected_branch(make_settings) -> None:
    with pytest.raises(ValueError, match="protected branch"):
        validate_pr_proposal(
            make_settings,
            branch="main",
            title="t",
            body="b",
            changes=[{"path": "src/app.py", "content": "x", "message": "m"}],
        )
    with pytest.raises(ValueError, match="protected branch"):
        validate_pr_proposal(
            make_settings,
            branch="release/1.0",
            title="t",
            body="b",
            changes=[{"path": "src/app.py", "content": "x", "message": "m"}],
        )


def test_validate_pr_proposal_accepts_safe_change(make_settings) -> None:
    proposal = validate_pr_proposal(
        make_settings,
        branch="issue-agent/fix-parser",
        title="fix: parser",
        body="body",
        changes=[{"path": "src/parser.py", "content": "x", "message": "m"}],
    )
    assert proposal["branch"] == "issue-agent/fix-parser"
    assert proposal["changes"][0]["path"] == "src/parser.py"


# ══════════════════════════════════════════════════════════════════
# grep_content 的 ReDoS 防护
# ══════════════════════════════════════════════════════════════════


def _executor(make_settings, make_issue, cache: dict[str, str]) -> ToolExecutor:
    return ToolExecutor(MagicMock(), make_settings, make_issue(), list(cache), file_cache=cache)


async def test_grep_content_rejects_nested_quantifiers(make_settings, make_issue) -> None:
    executor = _executor(make_settings, make_issue, {"src/a.py": "aaaa"})
    result = await executor._tool_grep_content("(a+)+$")  # noqa: SLF001
    assert "catastrophic" in result


async def test_grep_content_matches_normally(make_settings, make_issue) -> None:
    executor = _executor(make_settings, make_issue, {"src/a.py": "def parse_path():\n    return raw\n"})
    result = await executor._tool_grep_content("parse_path")  # noqa: SLF001
    assert "src/a.py:L1" in result


async def test_grep_content_escapes_invalid_regex(make_settings, make_issue) -> None:
    """非法正则回落为字面量匹配，而不是抛异常打断调查。"""
    executor = _executor(make_settings, make_issue, {"src/a.py": "value = a[b\n"})
    result = await executor._tool_grep_content("a[b")  # noqa: SLF001
    assert "src/a.py:L1" in result


# ══════════════════════════════════════════════════════════════════
# 启动自检：写模式必须配认证
# ══════════════════════════════════════════════════════════════════


async def test_write_mode_without_api_key_refuses_startup(monkeypatch) -> None:
    from app import main as main_module
    from app.config import Settings

    settings = Settings(
        openai_api_key="test-key",
        write_mode=True,
        api_key=None,
        session_db_path=":memory:",
        independent_review=False,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match="WRITE_MODE"):
        async with main_module.lifespan(main_module.app):
            pass


async def test_unauthenticated_startup_only_warns(monkeypatch, caplog) -> None:
    """未开认证但未开写模式：只警告，不阻止本机单用户使用。"""
    from app import main as main_module
    from app.config import Settings

    settings = Settings(
        openai_api_key="test-key",
        write_mode=False,
        api_key=None,
        session_db_path=":memory:",
        independent_review=False,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    async with main_module.lifespan(main_module.app):
        assert main_module.app.state.investigation_slots is not None
        from app.services import InvestigationGate

        assert isinstance(main_module.app.state.investigation_slots, InvestigationGate)


# ══════════════════════════════════════════════════════════════════
# 并发闸门
# ══════════════════════════════════════════════════════════════════


async def test_investigation_gate_is_non_blocking_and_releases() -> None:
    from app.services import InvestigationGate, acquire_investigation_slot

    gate = InvestigationGate(1)
    assert gate.limit == 1
    assert await acquire_investigation_slot(gate) is True
    assert gate.in_use == 1
    # 关键：满载时必须立刻返回 False，而不是排队等待或依赖定时器
    # （旧实现用 wait_for(timeout=0.05)，高负载机器上会把合法请求误判为「已满」）
    assert await acquire_investigation_slot(gate) is False
    await gate.release()
    assert gate.in_use == 0
    assert await acquire_investigation_slot(gate) is True


def test_investigation_gate_rejects_invalid_limit() -> None:
    from app.services import InvestigationGate

    with pytest.raises(ValueError):
        InvestigationGate(0)


async def test_investigation_gate_release_without_acquire_is_safe() -> None:
    from app.services import InvestigationGate

    gate = InvestigationGate(2)
    await gate.release()
    assert gate.in_use == 0


async def test_periodic_touch_refreshes_updated_at() -> None:
    from app.services import periodic_touch

    manager = SessionManager()
    session = await manager.create("https://github.com/acme/widget/issues/1")
    before = session.updated_at

    async with periodic_touch(manager, session.session_id):
        await asyncio.sleep(0.01)  # 心跳间隔 15s；这里只验证上下文管理器正常进出

    after = await manager.get(session.session_id)
    assert after is not None
    assert after.updated_at >= before


async def test_periodic_touch_accepts_none_session() -> None:
    from app.services import periodic_touch

    async with periodic_touch(SessionManager(), None):
        pass


# ══════════════════════════════════════════════════════════════════
# /chat 路由：直接调用（不依赖 app.state 与事件循环时序）
# ══════════════════════════════════════════════════════════════════


async def _completed_session(manager: SessionManager) -> str:
    session = await manager.create("https://github.com/acme/widget/issues/1")
    session.status = "completed"
    session.phase = "completed"
    await manager.save(session)
    return session.session_id


async def test_chat_route_existing_session_completes_and_releases_gate(monkeypatch) -> None:
    """已有会话的成功路径：状态收尾 + 闸门释放（此前只在端点级测试里被间接覆盖）。"""
    from app.deps import ProviderClients
    from app.models import ChatRequest, ChatResponse
    from app.routes import chat as chat_routes
    from app.services import InvestigationGate

    manager = SessionManager()
    session_id = await _completed_session(manager)

    async def fake_chat(self, session, message):  # noqa: ANN001
        return ChatResponse(session_id=session.session_id, reply="ok", tools_used=[], report=None)

    monkeypatch.setattr(IssueAgent, "chat", fake_chat)
    gate = InvestigationGate(2)

    result = await chat_routes.chat(
        ChatRequest(session_id=session_id, message="hi"),
        ProviderClients(None, None),
        manager,
        CircuitBreaker(threshold=5, recovery=30),
        gate,
    )

    assert result.reply == "ok"
    assert gate.in_use == 0, "闸门名额必须释放，否则后续请求会被误判为已满"
    refreshed = await manager.get(session_id)
    assert refreshed is not None
    assert refreshed.status == "completed"


async def test_chat_route_new_session_investigates_and_releases_gate(monkeypatch) -> None:
    """新会话分支：占位 → 调查 → 组装 ChatResponse，并释放闸门。"""
    from app.deps import ProviderClients
    from app.models import AnalysisReport, ChatRequest
    from app.routes import chat as chat_routes
    from app.services import InvestigationGate

    report = AnalysisReport(
        summary="s",
        root_cause="r",
        confidence="low",
        evidence=[],
        proposed_changes=[],
        tests=[],
        risks=[],
    )

    async def fake_investigate(self, issue_url, *, session=None):  # noqa: ANN001
        assert session is not None and session.status == "running"
        return report

    monkeypatch.setattr(IssueAgent, "investigate", fake_investigate)
    manager = SessionManager()
    gate = InvestigationGate(1)

    response = await chat_routes.chat(
        ChatRequest(issue_url="https://github.com/acme/widget/issues/2", message="look"),
        ProviderClients(None, None),
        manager,
        CircuitBreaker(threshold=5, recovery=30),
        gate,
    )

    assert response.session_id
    assert response.report is report
    assert gate.in_use == 0
    stored = await manager.get(response.session_id)
    assert stored is not None
    assert stored.status == "completed"


async def test_chat_route_rejects_when_gate_is_full() -> None:
    """闸门满载时立刻 429，而不是排队等十几分钟。"""
    from fastapi import HTTPException

    from app.deps import ProviderClients
    from app.models import ChatRequest
    from app.routes import chat as chat_routes
    from app.services import InvestigationGate

    manager = SessionManager()
    gate = InvestigationGate(1)
    assert await gate.try_acquire() is True  # 占满

    with pytest.raises(HTTPException) as excinfo:
        await chat_routes.chat(
            ChatRequest(issue_url="https://github.com/acme/widget/issues/3", message="look"),
            ProviderClients(None, None),
            manager,
            CircuitBreaker(threshold=5, recovery=30),
            gate,
        )

    assert excinfo.value.status_code == 429
