import json
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("SESSION_DB_PATH", ":memory:")

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.agent import IssueAgent, ModelResponseError
from app.build import BUILD_ID
from app.config import Settings
from app.events import done_event, phase_event, tool_call_event, tool_result_event
from app.github import GitHubError, GitHubRateLimitError
from app.main import app, get_session_manager, get_settings
from app.models import AnalysisReport, ApplyFixRequest, ChatResponse, IssueData
from app.sessions import SessionManager


def test_health() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "issue-agent", "build_id": BUILD_ID}


async def test_lifespan_initializes_and_closes_session_manager() -> None:
    """Verify lifespan creates a SessionManager on app.state and closes it on shutdown."""
    async with main_module.lifespan(app):
        assert hasattr(app.state, "session_manager")
        assert isinstance(app.state.session_manager, SessionManager)
        assert app.state.openai_client is not None
        assert app.state.github_client is not None

    assert app.state.session_manager is None
    assert app.state.openai_client is None
    assert app.state.github_client is None


def test_web_ui_renders() -> None:
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "GitHub Issue Agent" in response.text
    assert 'id="conversation"' in response.text
    assert 'id="report-panel"' in response.text
    assert 'id="report-toggle"' in response.text
    assert 'id="history-list"' in response.text
    assert 'id="history-search"' in response.text
    assert 'id="back-button"' in response.text
    assert 'id="cancel-analysis"' in response.text
    assert 'class="brand-identity"' in response.text
    assert "/static/css/primer.css" in response.text
    assert "/static/js/core.js" in response.text
    assert "/static/js/app.js" in response.text
    assert f"?v={BUILD_ID}" in response.text
    assert 'meta name="issue-agent-build"' in response.text
    assert '<script src="/static/vendor/echarts.min.js' not in response.text
    assert '<script src="/static/vendor/highlight.min.js' not in response.text


def test_static_frontend_modules_are_served() -> None:
    client = TestClient(app)
    script = client.get("/static/js/core.js")
    runtime = client.get("/static/js/session-runtime.js")
    app_script = client.get("/static/js/app.js")
    stylesheet = client.get("/static/css/primer.css")

    assert script.status_code == 200
    assert "window.IssueAgent" in script.text
    assert "IA.Runtime" in runtime.text
    assert runtime.status_code == 200
    assert app_script.status_code == 200
    assert 'case "review"' in app_script.text
    assert "loadSessions();" in app_script.text
    assert stylesheet.status_code == 200
    assert "#report-panel" in stylesheet.text
    assert ".investigation-timeline" in stylesheet.text
    assert ".review-chip" in stylesheet.text
    assert "--canvas-inset" in stylesheet.text
    assert ".brand-identity" in stylesheet.text


def test_compression_and_versioned_static_cache_headers() -> None:
    client = TestClient(app)
    page = client.get("/", headers={"Accept-Encoding": "gzip"})
    versioned = client.get(f"/static/js/app.js?v={BUILD_ID}", headers={"Accept-Encoding": "gzip"})
    unversioned = client.get("/static/js/app.js")

    assert page.headers.get("Content-Encoding") == "gzip"
    assert versioned.headers.get("Content-Encoding") == "gzip"
    assert versioned.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert unversioned.headers["Cache-Control"] == "public, max-age=0, must-revalidate"


def test_analyze_maps_invalid_model_response_to_bad_gateway(monkeypatch) -> None:
    async def fail(self: IssueAgent, issue_url: str, **kwargs):
        raise ModelResponseError("The model returned an invalid analysis report")

    monkeypatch.setattr(IssueAgent, "investigate", fail)
    app.dependency_overrides[get_settings] = lambda: Settings(openai_api_key="test-key")
    try:
        response = TestClient(app).post(
            "/analyze",
            json={"issue_url": "https://github.com/acme/widget/issues/1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json() == {"detail": "The model returned an invalid analysis report"}


def test_analyze_maps_rate_limit_to_429(monkeypatch) -> None:
    async def fail(self: IssueAgent, issue_url: str, **kwargs):
        raise GitHubRateLimitError("rate limit hit")

    monkeypatch.setattr(IssueAgent, "investigate", fail)
    app.dependency_overrides[get_settings] = lambda: Settings(openai_api_key="test-key")
    try:
        response = TestClient(app).post(
            "/analyze",
            json={"issue_url": "https://github.com/acme/widget/issues/1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 429
    assert response.json() == {"detail": "rate limit hit"}


def test_analyze_maps_github_error_to_bad_gateway(monkeypatch) -> None:
    async def fail(self: IssueAgent, issue_url: str, **kwargs):
        raise GitHubError("not found")

    monkeypatch.setattr(IssueAgent, "investigate", fail)
    app.dependency_overrides[get_settings] = lambda: Settings(openai_api_key="test-key")
    try:
        response = TestClient(app).post(
            "/analyze",
            json={"issue_url": "https://github.com/acme/widget/issues/1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json() == {"detail": "not found"}


def test_chat_requires_issue_url_for_new_session(monkeypatch) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(openai_api_key="test-key")
    try:
        response = TestClient(app).post(
            "/chat",
            json={"message": "hello"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert "issue_url" in response.json()["detail"]


def test_chat_returns_404_for_unknown_session(monkeypatch) -> None:
    manager = SessionManager()
    app.dependency_overrides[get_settings] = lambda: Settings(openai_api_key="test-key")
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        response = TestClient(app).post(
            "/chat",
            json={"session_id": "nonexistent", "message": "hello"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert "Session not found" in response.json()["detail"]


def test_chat_new_session_returns_report(monkeypatch) -> None:
    report = AnalysisReport(
        summary="测试摘要",
        root_cause="测试根因",
        confidence="high",
        evidence=[],
        proposed_changes=["修复 A"],
        patch=None,
        tests=["测试 1"],
        risks=[],
    )

    async def fake_investigate(self: IssueAgent, issue_url: str, *, session=None):
        if session is not None:
            session.report = report
        return report

    manager = SessionManager()
    monkeypatch.setattr(IssueAgent, "investigate", fake_investigate)
    app.dependency_overrides[get_settings] = lambda: Settings(openai_api_key="test-key")
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        response = TestClient(app).post(
            "/chat",
            json={
                "issue_url": "https://github.com/acme/widget/issues/1",
                "message": "分析一下",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert "session_id" in data
    assert "测试摘要" in data["reply"]
    assert data["report"]["confidence"] == "high"


def test_stream_creates_session_that_can_continue_in_chat(monkeypatch) -> None:
    manager = SessionManager()
    app.dependency_overrides[get_session_manager] = lambda: manager

    async def fake_stream(self: IssueAgent, issue_url: str, *, session=None):
        yield done_event()

    async def fake_chat(self: IssueAgent, session, message: str):
        return ChatResponse(session_id=session.session_id, reply=f"reply: {message}")

    monkeypatch.setattr(IssueAgent, "investigate_stream", fake_stream)
    monkeypatch.setattr(IssueAgent, "chat", fake_chat)
    client = TestClient(app)

    stream_response = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
    session_line = next(line for line in stream_response.text.splitlines() if '"type": "session"' in line)
    session_id = json.loads(session_line.removeprefix("data: "))["data"]["session_id"]
    chat_response = client.post("/chat", json={"session_id": session_id, "message": "what changed?"})

    assert chat_response.status_code == 200
    assert chat_response.json()["reply"] == "reply: what changed?"
    app.dependency_overrides.pop(get_session_manager, None)


def test_stream_persists_metrics_on_tool_call_events(monkeypatch) -> None:
    """Tool-call events must trigger lightweight metrics persistence so the
    frontend session list / detail view shows live progress (model_calls,
    tool_calls, files_read) instead of zeros until the next phase transition.
    """
    manager = SessionManager()
    app.dependency_overrides[get_session_manager] = lambda: manager

    async def fake_stream(self: IssueAgent, issue_url: str, *, session=None):
        # Simulate one tool-call round-trip with metrics increments, then done.
        if session is not None:
            session.metrics["tool_calls"] = 1
            session.metrics["model_calls"] = 1
        yield tool_call_event("read_file", {"path": "src/a.py"}, iteration=1)
        yield tool_result_event("read_file", "L1: content")
        yield done_event()

    monkeypatch.setattr(IssueAgent, "investigate_stream", fake_stream)
    client = TestClient(app)

    stream_response = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/1"})
    session_line = next(line for line in stream_response.text.splitlines() if '"type": "session"' in line)
    session_id = json.loads(session_line.removeprefix("data: "))["data"]["session_id"]

    # After the stream completes, the session detail must reflect the
    # tool_call metrics that were persisted mid-stream, not zeros.
    detail = client.get(f"/session/{session_id}").json()
    assert detail["metrics"]["tool_calls"] == 1
    assert detail["metrics"]["model_calls"] == 1
    app.dependency_overrides.pop(get_session_manager, None)


def test_apply_fix_requires_explicit_confirmation() -> None:
    assert ApplyFixRequest().confirm is False


async def test_apply_fix_new_and_legacy_routes_require_confirmation(monkeypatch) -> None:
    manager = SessionManager()
    app.dependency_overrides[get_session_manager] = lambda: manager
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(openai_api_key="test-key", write_mode=True),
    )
    session = await manager.create("https://github.com/acme/widget/issues/42")
    session.issue = IssueData(
        owner="acme",
        repo="widget",
        number=42,
        title="Parser bug",
        body="",
        labels=["bug"],
        comments=[],
        default_branch="main",
    )
    await manager.save(session)
    await manager.save_pr_proposal(
        session.session_id,
        {
            "branch": "fix/parser-bug",
            "title": "fix: guard empty parser input",
            "body": "Prevents the parser crash.",
            "changes": [{"path": "src/parser.py", "content": "fixed\n", "message": "fix parser"}],
        },
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        current = await client.post(f"/session/{session.session_id}/apply-fix", json={"confirm": False})
        legacy = await client.post(f"/apply-fix?session_id={session.session_id}", json={"confirm": False})

    assert current.status_code == 400
    assert current.json() == {"detail": "Set confirm=true to create the PR"}
    assert legacy.status_code == 400
    assert legacy.json() == current.json()
    app.dependency_overrides.pop(get_session_manager, None)


def test_apply_fix_is_disabled_by_default() -> None:
    response = TestClient(app).post("/session/missing/apply-fix", json={"confirm": True})

    assert response.status_code == 403
    assert response.json() == {"detail": "Write mode is disabled"}


def test_openapi_exposes_only_the_session_scoped_apply_fix_route() -> None:
    paths = TestClient(app).get("/openapi.json").json()["paths"]

    assert "/session/{session_id}/apply-fix" in paths
    assert "/apply-fix" not in paths


async def test_session_history_api_supports_restore_rename_archive_and_delete(monkeypatch) -> None:
    manager = SessionManager()
    app.dependency_overrides[get_session_manager] = lambda: manager
    session = await manager.create("https://github.com/acme/widget/issues/42")
    session.issue = IssueData(
        owner="acme",
        repo="widget",
        number=42,
        title="Parser crashes on empty input",
        body="",
        labels=["bug"],
        comments=[],
        default_branch="main",
    )
    session.status = "completed"
    session.messages = [{"role": "user", "content": "Why does this fail?"}]
    await manager.save(session)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        listing = await client.get("/sessions")
        detail = await client.get(f"/session/{session.session_id}")
        renamed = await client.patch(
            f"/session/{session.session_id}",
            json={"display_title": "Critical parser bug"},
        )
        archived = await client.patch(
            f"/session/{session.session_id}",
            json={"archived": True},
        )
        active_listing = await client.get("/sessions")
        archive_listing = await client.get("/sessions?archived=true")
        deleted = await client.delete(f"/session/{session.session_id}")

    assert listing.json()[0]["title"] == "Parser crashes on empty input"
    assert detail.json()["messages"][0]["content"] == "Why does this fail?"
    assert renamed.json()["title"] == "Critical parser bug"
    assert archived.json()["archived"] is True
    assert active_listing.json() == []
    assert archive_listing.json()[0]["session_id"] == session.session_id
    assert deleted.status_code == 204
    app.dependency_overrides.pop(get_session_manager, None)


async def test_session_detail_exposes_durable_events_and_metrics(monkeypatch) -> None:
    manager = SessionManager()
    app.dependency_overrides[get_session_manager] = lambda: manager
    session = await manager.create("https://github.com/acme/widget/issues/7")
    session.metrics = {"model_calls": 3, "duration_ms": 1200}
    await manager.save(session)
    await manager.append_event(
        session.session_id,
        {"type": "phase", "data": {"phase": "verifying", "label": "Verifying"}, "message": ""},
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/session/{session.session_id}")

    assert response.status_code == 200
    assert response.json()["metrics"]["model_calls"] == 3
    assert response.json()["events"][0]["data"]["phase"] == "verifying"
    app.dependency_overrides.pop(get_session_manager, None)


async def test_cancel_endpoint_marks_running_session_for_cancellation(monkeypatch) -> None:
    manager = SessionManager()
    app.dependency_overrides[get_session_manager] = lambda: manager
    session = await manager.create("https://github.com/acme/widget/issues/8")
    session.status = "running"
    await manager.save(session)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/session/{session.session_id}/cancel")

    assert response.status_code == 200
    assert await manager.is_cancel_requested(session.session_id) is True
    app.dependency_overrides.pop(get_session_manager, None)


def test_stream_persists_phase_events(monkeypatch) -> None:
    manager = SessionManager()
    app.dependency_overrides[get_session_manager] = lambda: manager

    async def fake_stream(self: IssueAgent, issue_url: str, *, session=None):
        yield phase_event("exploring", "Exploring repository")
        yield done_event()

    monkeypatch.setattr(IssueAgent, "investigate_stream", fake_stream)
    client = TestClient(app)
    response = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/9"})
    session_line = next(line for line in response.text.splitlines() if '"type": "session"' in line)
    session_id = json.loads(session_line.removeprefix("data: "))["data"]["session_id"]

    detail = client.get(f"/session/{session_id}").json()
    assert [event["type"] for event in detail["events"]] == ["session", "phase", "done"]
    assert detail["status"] == "completed"
    app.dependency_overrides.pop(get_session_manager, None)


def test_stream_honors_cooperative_cancellation(monkeypatch) -> None:
    manager = SessionManager()
    app.dependency_overrides[get_session_manager] = lambda: manager

    async def fake_stream(self: IssueAgent, issue_url: str, *, session=None):
        assert session is not None
        await manager.request_cancel(session.session_id)
        yield phase_event("exploring", "Exploring repository")

    monkeypatch.setattr(IssueAgent, "investigate_stream", fake_stream)
    client = TestClient(app)
    response = client.post("/stream", json={"issue_url": "https://github.com/acme/widget/issues/10"})
    session_line = next(line for line in response.text.splitlines() if '"type": "session"' in line)
    session_id = json.loads(session_line.removeprefix("data: "))["data"]["session_id"]

    assert '"type": "cancelled"' in response.text
    detail = client.get(f"/session/{session_id}").json()
    assert detail["status"] == "cancelled"
    assert detail["phase"] == "cancelled"
    app.dependency_overrides.pop(get_session_manager, None)


def test_settings_is_frozen_and_rejects_mutation() -> None:
    """Frozen Settings prevents accidental runtime mutation."""
    settings = Settings(openai_api_key="test-key")
    try:
        settings.openai_model = "different-model"
        raise AssertionError("Should have raised ValidationError")
    except Exception as error:
        assert "frozen" in str(error).lower() or "immutable" in str(error).lower() or "instance" in str(error).lower()


def _proposal() -> dict:
    return {
        "branch": "fix/issue-1",
        "title": "Fix the bug",
        "body": "Explain the fix",
        "changes": [{"path": "src/a.py", "content": "x = 1", "message": "fix: apply"}],
    }


class _ConfirmRequest:
    def __init__(self) -> None:
        self.confirm = True


async def _apply_fix_fixture(tmp_path, manager=None):
    """构造一个带校验通过提案的 completed 会话，返回 (manager, settings, session_id)。"""
    from app.config import Settings as AppSettings

    if manager is None:
        manager = SessionManager(db_path=str(tmp_path / "apply.db"))
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
    session.phase = "completed"
    await manager.save(session)
    await manager.save_pr_proposal(session.session_id, _proposal())
    settings = AppSettings(openai_api_key="test-key", write_mode=True)
    return manager, settings, session.session_id


async def test_apply_fix_does_not_delete_branch_when_pr_was_created(monkeypatch, tmp_path) -> None:
    """PR 已创建但响应校验失败（GitHubPRCreatedError）：不得回滚删除分支。"""
    from fastapi import HTTPException

    from app.github import GitHubPRCreatedError
    from app.services import apply_fix

    deleted_branches: list[str] = []
    pr_attempted = False

    class FakeGitHub:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_branch_sha(self, owner, repo, base):
            return "a" * 40

        async def create_branch(self, owner, repo, branch, sha):
            pass

        async def create_or_update_file(self, *args, **kwargs):
            return {}

        async def create_pull_request(self, *args, **kwargs):
            nonlocal pr_attempted
            pr_attempted = True
            raise GitHubPRCreatedError("GitHub created the pull request but returned an invalid URL")

        async def delete_branch(self, owner, repo, branch):
            deleted_branches.append(branch)

    monkeypatch.setattr("app.services.GitHubClient", FakeGitHub)
    manager, settings, session_id = await _apply_fix_fixture(tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        await apply_fix(
            session_id=session_id,
            request=_ConfirmRequest(),
            settings=settings,
            session_mgr=manager,
        )
    assert exc_info.value.status_code == 502
    assert pr_attempted, "PR 创建应已被调用"
    assert deleted_branches == [], "PR 已创建时绝不能删除分支（会关闭刚创建的 PR）"
    await manager.close()


async def test_apply_fix_rolls_back_branch_on_generic_failure(monkeypatch, tmp_path) -> None:
    """普通 GitHubError（PR 未创建）：回滚删除已建分支。"""
    from fastapi import HTTPException

    from app.github import GitHubError
    from app.services import apply_fix

    deleted_branches: list[str] = []

    class FakeGitHub:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_branch_sha(self, owner, repo, base):
            return "a" * 40

        async def create_branch(self, owner, repo, branch, sha):
            pass

        async def create_or_update_file(self, *args, **kwargs):
            return {}

        async def create_pull_request(self, *args, **kwargs):
            raise GitHubError("boom")

        async def delete_branch(self, owner, repo, branch):
            deleted_branches.append(branch)

    monkeypatch.setattr("app.services.GitHubClient", FakeGitHub)
    manager, settings, session_id = await _apply_fix_fixture(tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        await apply_fix(
            session_id=session_id,
            request=_ConfirmRequest(),
            settings=settings,
            session_mgr=manager,
        )
    assert exc_info.value.status_code == 502
    assert deleted_branches == ["fix/issue-1"], "PR 未创建时分支应被回滚删除"
    await manager.close()


# ── 低覆盖分支补充：设置覆盖 / 限流 / 归档会话 / 导入边界 / 批量路由 ──


def test_resolve_override_settings_forks_without_mutating_global() -> None:
    from app.main import resolve_override_settings

    class FakeReq:
        language = "en"
        model = "custom-model"
        thinking = "disabled"
        reasoning_effort = "max"
        review = False

    base = get_settings()
    forked = resolve_override_settings(FakeReq())
    assert forked is not base
    assert forked.language == "en"
    assert forked.openai_model == "custom-model"
    assert forked.openai_thinking == "disabled"
    assert forked.openai_reasoning_effort == "max"
    assert forked.independent_review is False
    # 全局单例未被修改
    assert base.language == get_settings().language


def test_resolve_override_settings_no_overrides_returns_singleton() -> None:
    from app.main import resolve_override_settings

    class FakeReq:
        language = None
        model = None
        thinking = None
        reasoning_effort = None
        review = None

    assert resolve_override_settings(FakeReq()) is get_settings()


def test_rate_limit_exceeds_threshold_raises_429(monkeypatch) -> None:
    import asyncio

    from fastapi import HTTPException

    from app.config import Settings
    from app.main import _check_rate_limit, _rate_window_buckets

    _rate_window_buckets.clear()
    low_limit = Settings(openai_api_key="test-key", rate_limit_requests=2, rate_limit_window_seconds=60)
    monkeypatch.setattr("app.main.get_settings", lambda: low_limit)

    async def run() -> None:
        await _check_rate_limit("rl-key-1")
        await _check_rate_limit("rl-key-1")
        with pytest.raises(HTTPException) as exc_info:
            await _check_rate_limit("rl-key-1")
        assert exc_info.value.status_code == 429
        assert exc_info.value.headers["Retry-After"]

    asyncio.run(run())
    _rate_window_buckets.clear()


def test_rate_limit_cleans_stale_keys() -> None:
    """超过阈值时，窗口外的旧 key 被清理，字典不会无限膨胀。

    注意：不能通过 patch time.monotonic 模拟时间快进——asyncio 事件循环的
    loop.time() 也调用 time.monotonic()，会污染循环时钟。改为直接构造过期状态。
    """
    import asyncio
    from collections import deque

    from app.main import _check_rate_limit, _rate_window_buckets

    _rate_window_buckets.clear()
    # 101 个"远古时间戳"的 key：任何当前窗口都会判定为 stale
    for index in range(101):
        _rate_window_buckets[f"old-{index}"] = deque([1.0])

    async def run() -> None:
        await _check_rate_limit("fresh-key")  # 触发 len>100 的清理检查
        assert len(_rate_window_buckets) == 1
        assert "fresh-key" in _rate_window_buckets

    asyncio.run(run())
    _rate_window_buckets.clear()


def test_rate_limit_middleware_returns_429_json(monkeypatch) -> None:
    """限流超限时返回 JSON 429（而非 500），带 Retry-After 头。"""
    from app.config import Settings
    from app.main import get_session_manager as gsm
    from app.main import get_settings as original_get_settings

    low_limit = Settings(
        openai_api_key="test-key",
        rate_limit_requests=2,
        rate_limit_window_seconds=60,
    )
    monkeypatch.setattr("app.main.get_settings", lambda: low_limit)
    app.dependency_overrides[gsm] = lambda: SessionManager()  # /sessions 依赖（不碰 lock）
    client = TestClient(app)
    try:
        first = client.get("/sessions")
        second = client.get("/sessions")
        third = client.get("/sessions")
        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429
        assert third.json()["detail"].startswith("Rate limit exceeded")
        assert "retry-after" in {k.lower() for k in third.headers}
    finally:
        app.dependency_overrides.pop(gsm, None)
        monkeypatch.setattr("app.main.get_settings", original_get_settings)


async def test_chat_archived_session_returns_409() -> None:
    """归档会话继续 chat 返回 409。"""
    import httpx as httpx_client

    from app.main import get_session_manager as gsm

    async with main_module.lifespan(app):
        manager = app.state.session_manager
        app.dependency_overrides[gsm] = lambda: manager
        try:
            session = await manager.create("https://github.com/acme/widget/issues/1")
            session.archived_at = "2026-01-01T00:00:00+00:00"
            await manager.save(session)
            transport = httpx_client.ASGITransport(app=app)
            async with httpx_client.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/chat", json={"session_id": session.session_id, "message": "hi"}
                )
            assert response.status_code == 409
            assert "Restore" in response.json()["detail"]
        finally:
            app.dependency_overrides.pop(gsm, None)


def _with_session_manager():
    """为依赖 get_session_manager 的端点提供 manager（TestClient 无 lifespan）。"""
    from app.main import get_session_manager as gsm

    app.dependency_overrides[gsm] = lambda: SessionManager()
    return gsm


def test_import_rejects_oversized_payload() -> None:
    gsm = _with_session_manager()
    client = TestClient(app)
    try:
        response = client.post(
            "/session/import",
            headers={"Content-Length": str(10 * 1024 * 1024)},
            content=b"{}",
        )
        assert response.status_code == 413
    finally:
        app.dependency_overrides.pop(gsm, None)


def test_import_rejects_invalid_json() -> None:
    gsm = _with_session_manager()
    client = TestClient(app)
    try:
        response = client.post("/session/import", content=b"not json{{{")
        assert response.status_code == 400
    finally:
        app.dependency_overrides.pop(gsm, None)


def test_import_rejects_wrong_format() -> None:
    gsm = _with_session_manager()
    client = TestClient(app)
    try:
        response = client.post("/session/import", json={"format": "other"})
        assert response.status_code == 400
        assert "Not a valid issue-agent session export" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(gsm, None)


def test_import_rejects_missing_issue_url() -> None:
    gsm = _with_session_manager()
    client = TestClient(app)
    try:
        response = client.post("/session/import", json={"format": "issue-agent-session", "session": {}})
        assert response.status_code == 400
        assert "issue_url" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(gsm, None)


def test_batch_submit_invalid_url_returns_422() -> None:
    from app.task_queue import TaskQueue

    queue = TaskQueue(get_settings(), None, max_concurrent=1, max_queue_size=10)
    app.state.task_queue = queue
    client = TestClient(app)
    response = client.post("/batch", json={"issue_urls": ["not-a-url"]})
    assert response.status_code == 422
    assert "issue_url must" in response.json()["detail"]


def test_batch_status_unknown_returns_404() -> None:
    from app.task_queue import TaskQueue

    queue = TaskQueue(get_settings(), None, max_concurrent=1, max_queue_size=10)
    app.state.task_queue = queue
    client = TestClient(app)
    response = client.get("/batch/does-not-exist")
    assert response.status_code == 404


def test_analyze_invalid_url_returns_422(monkeypatch) -> None:
    """analyze 对 URL 解析失败返回 422。"""
    from app.agent import IssueAgent as AgentClass

    original = AgentClass.investigate

    async def raise_value_error(self, issue_url, *, session=None):
        raise ValueError("issue_url must be an https://github.com URL")

    monkeypatch.setattr(AgentClass, "investigate", raise_value_error)
    client = TestClient(app)
    response = client.post("/analyze", json={"issue_url": "https://github.com/a/b/issues/1"})
    assert response.status_code == 422
    monkeypatch.setattr(AgentClass, "investigate", original)
