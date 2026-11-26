"""Batch analysis API: submission, repository expansion, results, and retry."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app import rate_limit
from app.circuit_breaker import CircuitBreaker
from app.config import get_settings
from app.github import GitHubClient, GitHubError, GitHubRateLimitError, GitHubResourceError
from app.main import app
from app.models import AnalysisReport
from app.routes.batch import REPO_ISSUE_LIMIT, parse_repository_url
from app.task_queue import TaskQueue

_ISSUE_1 = "https://github.com/acme/widget/issues/1"
_ISSUE_2 = "https://github.com/acme/widget/issues/2"
_REPO = "https://github.com/acme/widget"


class FakeGitHub:
    """Minimal GitHubClient stand-in for repository expansion."""

    def __init__(
        self,
        numbers: dict[str, list[int]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._numbers = numbers or {}
        self._error = error
        self.calls: list[tuple[str, str, int]] = []
        self.closed = False

    async def list_open_issues(self, owner: str, repo: str, limit: int = 10) -> list[int]:
        self.calls.append((owner, repo, limit))
        if self._error is not None:
            raise self._error
        return self._numbers.get(f"{owner}/{repo}", [])

    async def aclose(self) -> None:
        self.closed = True


def _report() -> AnalysisReport:
    return AnalysisReport(
        summary="Parser crashes",
        root_cause="Missing guard",
        confidence="high",
        evidence=[],
        proposed_changes=["Add a guard"],
        tests=["Add a regression test"],
        risks=[],
    )


@pytest.fixture(autouse=True)
def _reset_rate_limit() -> Iterator[None]:
    """Keep this module's request volume from tripping other modules' rate limits."""
    rate_limit._rate_window_buckets.clear()
    yield
    rate_limit._rate_window_buckets.clear()


@pytest.fixture
def queue() -> TaskQueue:
    return TaskQueue(get_settings(), CircuitBreaker(threshold=5, recovery=30), max_concurrent=1, max_queue_size=10)


@pytest.fixture
def client(queue: TaskQueue) -> Iterator[TestClient]:
    app.state.task_queue = queue
    app.state.github_client = None
    yield TestClient(app)
    app.state.task_queue = None
    app.state.github_client = None


def test_submit_issue_urls_creates_pending_tasks(client: TestClient) -> None:
    response = client.post("/batch", json={"issue_urls": [_ISSUE_1, _ISSUE_2]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["progress"]["pending"] == 2
    assert [task["issue_url"] for task in payload["tasks"]] == [_ISSUE_1, _ISSUE_2]
    assert all(task["status"] == "pending" and task["has_report"] is False for task in payload["tasks"])


def test_submit_rejects_invalid_issue_url_with_index(client: TestClient) -> None:
    response = client.post("/batch", json={"issue_urls": [_ISSUE_1, "not-a-url"]})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "issue_urls[1]" in detail
    assert "issue_url must" in detail


def test_submit_rejects_empty_request(client: TestClient) -> None:
    response = client.post("/batch", json={"issue_urls": [], "repository_urls": []})
    assert response.status_code == 422
    assert "At least one issue URL" in response.json()["detail"]


def test_submit_expands_repository_urls(client: TestClient, monkeypatch) -> None:
    fake = FakeGitHub({"acme/widget": [7, 8]})
    app.state.github_client = fake

    response = client.post("/batch", json={"issue_urls": [_ISSUE_1], "repository_urls": [_REPO + "/"]})

    assert response.status_code == 200
    urls = [task["issue_url"] for task in response.json()["tasks"]]
    assert urls == [
        _ISSUE_1,
        "https://github.com/acme/widget/issues/7",
        "https://github.com/acme/widget/issues/8",
    ]
    assert fake.calls == [("acme", "widget", REPO_ISSUE_LIMIT)]


def test_submit_deduplicates_issue_and_repository_results(client: TestClient) -> None:
    app.state.github_client = FakeGitHub({"acme/widget": [1, 1]})
    response = client.post("/batch", json={"issue_urls": [_ISSUE_1], "repository_urls": [_REPO]})
    assert response.status_code == 200
    assert [task["issue_url"] for task in response.json()["tasks"]] == [_ISSUE_1]


def test_submit_repository_without_open_issues_returns_422(client: TestClient) -> None:
    app.state.github_client = FakeGitHub({"acme/widget": []})
    response = client.post("/batch", json={"repository_urls": [_REPO]})
    assert response.status_code == 422
    assert "At least one issue URL" in response.json()["detail"]


def test_submit_rejects_invalid_repository_url(client: TestClient) -> None:
    response = client.post("/batch", json={"repository_urls": ["https://github.com/acme"]})
    assert response.status_code == 422
    assert "repository_urls[0]" in response.json()["detail"]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (GitHubRateLimitError("rate limited"), 429),
        (GitHubResourceError("missing"), 422),
        (GitHubError("boom"), 502),
    ],
)
def test_repository_expansion_maps_github_errors(client: TestClient, error: Exception, status: int) -> None:
    app.state.github_client = FakeGitHub(error=error)
    response = client.post("/batch", json={"repository_urls": [_REPO]})
    assert response.status_code == status
    assert "acme/widget" in response.json()["detail"]


def test_repository_expansion_builds_ephemeral_client(monkeypatch, client: TestClient) -> None:
    created: list[FakeGitHub] = []

    class Factory(FakeGitHub):
        def __init__(self, *_args, **_kwargs) -> None:
            super().__init__({"acme/widget": [5]})
            created.append(self)

    monkeypatch.setattr("app.routes.batch.GitHubClient", Factory)
    response = client.post("/batch", json={"repository_urls": [_REPO]})
    assert response.status_code == 200
    assert created and created[0].closed is True


def test_submit_rejects_more_than_batch_limit(client: TestClient) -> None:
    app.state.github_client = FakeGitHub({"acme/widget": list(range(1, 11))})
    issue_urls = [f"https://github.com/acme/other/issues/{number}" for number in range(1, 101)]
    response = client.post("/batch", json={"issue_urls": issue_urls, "repository_urls": [_REPO]})
    assert response.status_code == 422
    assert "at most 100" in response.json()["detail"]


def test_submit_queue_capacity_error_maps_to_422(client: TestClient, queue: TaskQueue) -> None:
    queue._max_queue_size = 1
    queue._pending.put_nowait(("other", "task"))
    response = client.post("/batch", json={"issue_urls": [_ISSUE_1]})
    assert response.status_code == 422
    assert "capacity exceeded" in response.json()["detail"]


def test_task_detail_returns_report(queue: TaskQueue) -> None:
    client = TestClient(app)
    app.state.task_queue = queue
    try:
        batch = queue.submit([_ISSUE_1])
        task = batch.tasks[0]
        task.status = "completed"
        task.result = _report()
        response = client.get(f"/batch/{batch.batch_id}/tasks/{task.task_id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["has_report"] is True
        assert payload["report"]["root_cause"] == "Missing guard"
    finally:
        app.state.task_queue = None


def test_task_detail_missing_batch_or_task(queue: TaskQueue) -> None:
    client = TestClient(app)
    app.state.task_queue = queue
    try:
        batch = queue.submit([_ISSUE_1])
        assert client.get("/batch/nope/tasks/whatever").status_code == 404
        assert client.get(f"/batch/{batch.batch_id}/tasks/unknown").status_code == 404
    finally:
        app.state.task_queue = None


def test_retry_requeues_failed_tasks(queue: TaskQueue) -> None:
    client = TestClient(app)
    app.state.task_queue = queue
    try:
        batch = queue.submit([_ISSUE_1, _ISSUE_2])
        batch.tasks[0].status = "failed"
        batch.tasks[0].error = "boom"
        response = client.post(f"/batch/{batch.batch_id}/retry")
        assert response.status_code == 200
        payload = response.json()
        assert payload["progress"]["pending"] == 2
        assert payload["tasks"][0]["status"] == "pending"
        assert payload["tasks"][0]["error"] is None
    finally:
        app.state.task_queue = None


def test_retry_unknown_batch_returns_404(queue: TaskQueue) -> None:
    client = TestClient(app)
    app.state.task_queue = queue
    try:
        assert client.post("/batch/nope/retry").status_code == 404
    finally:
        app.state.task_queue = None


def test_retry_without_failures_returns_409(queue: TaskQueue) -> None:
    client = TestClient(app)
    app.state.task_queue = queue
    try:
        batch = queue.submit([_ISSUE_1])
        batch.tasks[0].status = "completed"
        response = client.post(f"/batch/{batch.batch_id}/retry")
        assert response.status_code == 409
        assert "No failed tasks" in response.json()["detail"]
    finally:
        app.state.task_queue = None


def test_retry_respects_queue_capacity(queue: TaskQueue) -> None:
    client = TestClient(app)
    app.state.task_queue = queue
    try:
        batch = queue.submit([_ISSUE_1])
        batch.tasks[0].status = "failed"
        while queue.queue_size < queue._max_queue_size:
            queue._pending.put_nowait(("other", "task"))
        response = client.post(f"/batch/{batch.batch_id}/retry")
        assert response.status_code == 409
        assert "capacity exceeded" in response.json()["detail"]
    finally:
        app.state.task_queue = None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/widget", ("acme", "widget")),
        ("https://github.com/acme/widget/", ("acme", "widget")),
        ("https://github.com/acme/widget.git", ("acme", "widget")),
        ("https://www.github.com/acme/widget", ("acme", "widget")),
    ],
)
def test_parse_repository_url_accepts_common_forms(url: str, expected: tuple[str, str]) -> None:
    assert parse_repository_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/widget",
        "https://gitlab.com/acme/widget",
        "https://github.com/acme",
        f"{_ISSUE_1}",
    ],
)
def test_parse_repository_url_rejects_invalid_forms(url: str) -> None:
    with pytest.raises(ValueError):
        parse_repository_url(url)


async def test_list_open_issues_filters_pull_requests() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json=[
                {"number": 3, "title": "bug"},
                {"number": 2, "title": "PR", "pull_request": {"url": "x"}},
                {"number": 1, "title": "old bug"},
            ],
        )

    github = GitHubClient("token")
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    try:
        assert await github.list_open_issues("acme", "widget", 5) == [3, 1]
    finally:
        await github.aclose()
    assert "state=open" in seen["url"]
    assert "per_page=5" in seen["url"]


async def test_list_open_issues_rejects_unexpected_payload() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "not a list"})

    github = GitHubClient("token")
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubError):
            await github.list_open_issues("acme", "widget", 5)
    finally:
        await github.aclose()
