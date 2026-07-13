import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from fastapi.testclient import TestClient

from app.agent import IssueAgent, ModelResponseError
from app.config import Settings
from app.github import GitHubError, GitHubRateLimitError
from app.main import app, get_settings
from app.models import AnalysisReport


def test_health() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
