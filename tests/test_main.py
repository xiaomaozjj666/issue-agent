import json
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("SESSION_DB_PATH", ":memory:")

import httpx
from fastapi.testclient import TestClient

import app.main as main_module
from app.agent import IssueAgent, ModelResponseError
from app.build import BUILD_ID
from app.config import Settings
from app.events import done_event, phase_event
from app.github import GitHubError, GitHubRateLimitError
from app.main import app, get_settings
from app.models import AnalysisReport, ApplyFixRequest, ChatResponse, IssueData
from app.sessions import SessionManager


def test_health() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "issue-agent", "build_id": BUILD_ID}


async def test_lifespan_closes_global_session_manager(monkeypatch) -> None:
    manager = SessionManager()
    closed = False
    original_close = manager.close

    async def close() -> None:
        nonlocal closed
        closed = True
        await original_close()

    monkeypatch.setattr(manager, "close", close)
    monkeypatch.setattr(main_module, "_session_manager", manager)

    async with main_module.lifespan(app):
        pass

    assert closed is True
    assert main_module._session_manager is None


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


def test_static_frontend_modules_are_served() -> None:
    client = TestClient(app)
    script = client.get("/static/js/core.js")
    runtime = client.get("/static/js/session-runtime.js")
    app_script = client.get("/static/js/app.js")
    stylesheet = client.get("/static/css/primer.css")

    assert script.status_code == 200
    assert "window.apiJson" in script.text
    assert runtime.status_code == 200
    assert "window.cancelAnalysis" in runtime.text
    assert app_script.status_code == 200
    assert 'case "review"' in app_script.text
    assert "loadSessions();" in app_script.text
    assert stylesheet.status_code == 200
    assert "#report-panel" in stylesheet.text
    assert ".investigation-timeline" in stylesheet.text
    assert ".review-chip" in stylesheet.text
    assert "--canvas-inset" in stylesheet.text
    assert ".brand-identity" in stylesheet.text


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
    app.dependency_overrides[get_settings] = lambda: Settings(openai_api_key="test-key")
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

    monkeypatch.setattr(IssueAgent, "investigate", fake_investigate)
    app.dependency_overrides[get_settings] = lambda: Settings(openai_api_key="test-key")
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
    monkeypatch.setattr(main_module, "_session_manager", manager)

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


def test_apply_fix_requires_explicit_confirmation() -> None:
    assert ApplyFixRequest().confirm is False


async def test_apply_fix_new_and_legacy_routes_require_confirmation(monkeypatch) -> None:
    manager = SessionManager()
    monkeypatch.setattr(main_module, "_session_manager", manager)
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
    monkeypatch.setattr(main_module, "_session_manager", manager)
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


async def test_session_detail_exposes_durable_events_and_metrics(monkeypatch) -> None:
    manager = SessionManager()
    monkeypatch.setattr(main_module, "_session_manager", manager)
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


async def test_cancel_endpoint_marks_running_session_for_cancellation(monkeypatch) -> None:
    manager = SessionManager()
    monkeypatch.setattr(main_module, "_session_manager", manager)
    session = await manager.create("https://github.com/acme/widget/issues/8")
    session.status = "running"
    await manager.save(session)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/session/{session.session_id}/cancel")

    assert response.status_code == 200
    assert await manager.is_cancel_requested(session.session_id) is True


def test_stream_persists_phase_events(monkeypatch) -> None:
    manager = SessionManager()
    monkeypatch.setattr(main_module, "_session_manager", manager)

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


def test_stream_honors_cooperative_cancellation(monkeypatch) -> None:
    manager = SessionManager()
    monkeypatch.setattr(main_module, "_session_manager", manager)

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
