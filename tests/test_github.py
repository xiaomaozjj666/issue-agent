import json

import httpx
import pytest

from app.github import (
    GitHubClient,
    GitHubFileSkipped,
    GitHubRateLimitError,
    parse_issue_url,
    select_candidate_paths,
)
from app.models import IssueData


def test_parse_issue_url() -> None:
    assert parse_issue_url("https://github.com/acme/widget/issues/42") == ("acme", "widget", 42)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/widget/issues/42",
        "https://evil.example/acme/widget/issues/42",
        "https://github.com/acme/widget/pull/42",
    ],
)
def test_parse_issue_url_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(ValueError):
        parse_issue_url(url)


def test_select_candidate_paths_prefers_issue_terms_and_source() -> None:
    issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Parser crashes on empty token",
        body="The parse_token function fails",
        labels=["bug"],
        comments=[],
        default_branch="main",
    )
    paths = ["docs/parser.md", "src/parser.py", "src/database.py", "node_modules/parser.js"]

    assert select_candidate_paths(paths, issue, 2)[0] == "src/parser.py"


def test_select_candidate_paths_prefers_exact_path_tokens() -> None:
    issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Cache failure",
        body="Cache lookup fails",
        labels=[],
        comments=[],
        default_branch="main",
    )

    selected = select_candidate_paths(["src/cache.py", "src/cacheable_widget.py", "src/database.py"], issue, 2)

    assert selected[0] == "src/cache.py"


async def test_get_file_encodes_path_segments() -> None:
    issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="feature/test",
    )
    github = GitHubClient()
    request_url = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_url
        request_url = str(request.url)
        return httpx.Response(200, json={"encoding": "base64", "content": "cHJpbnQoMSk="})

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        source = await github.get_file(issue, "src/a file#.py")

    assert source.content == "print(1)"
    assert "/contents/src/a%20file%23.py" in request_url


async def test_rate_limit_response_raises_dedicated_error() -> None:
    github = GitHubClient()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"message": "rate limit exceeded"},
            headers={"X-RateLimit-Reset": "1700000000"},
        )

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        with pytest.raises(GitHubRateLimitError):
            await github.get_issue("acme", "widget", 1)


async def test_get_file_skips_oversized_file() -> None:
    issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
    )
    github = GitHubClient(max_file_bytes=100)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"encoding": "base64", "content": "cHJpbnQoMSk=", "size": 9999})

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        with pytest.raises(GitHubFileSkipped):
            await github.get_file(issue, "src/huge.py")


async def test_write_flow_uses_branch_sha_and_existing_file_sha() -> None:
    github = GitHubClient(write_enabled=True)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/branches/main"):
            return httpx.Response(200, json={"commit": {"sha": "a" * 40}})
        if request.method == "POST" and request.url.path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": "refs/heads/fix/issue-1"})
        if request.method == "GET" and "/contents/src/a.py" in request.url.path:
            return httpx.Response(200, json={"sha": "existing-file-sha"})
        if request.method == "PUT" and "/contents/src/a.py" in request.url.path:
            return httpx.Response(200, json={"content": {"sha": "new-file-sha"}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        base_sha = await github.get_branch_sha("acme", "widget", "main")
        await github.create_branch("acme", "widget", "fix/issue-1", base_sha)
        await github.create_or_update_file("acme", "widget", "src/a.py", "fixed\n", "fix/issue-1", "fix: parser")

    branch_payload = json.loads(requests[1].content)
    update_payload = json.loads(requests[-1].content)
    assert branch_payload["sha"] == "a" * 40
    assert update_payload["sha"] == "existing-file-sha"


def test_select_candidate_paths_filters_stop_words() -> None:
    issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="The bug is that this does not work with parser",
        body="Please help with the issue, thanks",
        labels=[],
        comments=[],
        default_branch="main",
    )
    paths = [
        "src/the.py",
        "src/please.py",
        "src/parser.py",
    ]

    selected = select_candidate_paths(paths, issue, 3)
    assert selected[0] == "src/parser.py"
    assert "src/the.py" not in selected[:1]
    assert "src/please.py" not in selected[:1]


def test_select_candidate_paths_empty_issue_text() -> None:
    issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
    )
    paths = ["src/a.py", "src/b.py", "docs/readme.md"]

    selected = select_candidate_paths(paths, issue, 3)
    assert len(selected) == 2
    assert all(p.endswith(".py") for p in selected)
