import json
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("SESSION_DB_PATH", ":memory:")

import httpx
from fastapi.testclient import TestClient

import app.main as main_module
from app.agent import IssueAgent, ModelResponseError
from app.config import Settings
from app.events import done_event
from app.github import GitHubError, GitHubRateLimitError
from app.main import app, get_settings
from app.models import AnalysisReport, ApplyFixRequest, ChatResponse, IssueData
from app.sessions import SessionManager


def test_health() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_web_ui_renders() -> None:
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "GitHub Issue Agent" in response.text
    assert 'id="conversation"' in response.text
    assert 'id="report-panel"' in response.text
    assert 'id="report-toggle"' in response.text
    assert 'id="history-list"' in response.text
    assert 'id="history-search"' in response.text


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
