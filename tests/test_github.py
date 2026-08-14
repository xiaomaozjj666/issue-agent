import json

import httpx
import pytest

from app.github import (
    GitHubClient,
    GitHubError,
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


async def test_429_always_raises_rate_limit_error() -> None:
    """429 即使不带限流头也一定是限流。"""
    github = GitHubClient()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "too many requests"})

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        with pytest.raises(GitHubRateLimitError):
            await github.get_issue("acme", "widget", 1)


async def test_403_without_rate_limit_headers_is_not_rate_limit_error() -> None:
    """403 无限流头（权限不足/资源不可访问）应抛通用 GitHubError，而非误导性的限流错误。"""
    github = GitHubClient()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        with pytest.raises(GitHubError) as exc_info:
            await github.get_issue("acme", "widget", 1)
        assert not isinstance(exc_info.value, GitHubRateLimitError)
        assert "403" in str(exc_info.value)


def _issue_endpoint_handler(success_response: dict | None = None, failure: str | None = None, fail_count: int = 0):
    """Build a MockTransport handler that serves the endpoints used by get_issue.

    The first ``fail_count`` calls to the issue endpoint trigger ``failure``
    (either a 5xx response or a raised transport error); subsequent calls
    succeed. Repo, branches, and comments endpoints always succeed. The returned
    handler carries a ``state`` dict with ``issue_calls`` for assertion.
    """
    success_response = success_response or {"title": "ok", "body": "", "labels": [], "state": "open"}
    state = {"issue_calls": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/comments" in url:
            return httpx.Response(200, json=[])
        if "/branches/" in url:
            return httpx.Response(200, json={"commit": {"sha": "abc123def456"}})
        if url.endswith("/repos/acme/widget") or url.endswith("/repos/acme/widget/"):
            return httpx.Response(200, json={"default_branch": "main"})
        # Issue endpoint
        state["issue_calls"] += 1
        if state["issue_calls"] <= fail_count and failure is not None:
            if failure == "5xx":
                return httpx.Response(503, json={"message": "service unavailable"})
            if failure == "timeout":
                raise httpx.ReadTimeout("read timed out")
            if failure == "connect":
                raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=success_response)

    handler.state = state  # type: ignore[attr-defined]
    return handler


async def test_get_retries_5xx_then_succeeds() -> None:
    """Transient 5xx responses are retried with exponential back-off."""
    github = GitHubClient(max_retries=3)
    handler = _issue_endpoint_handler(failure="5xx", fail_count=2)

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        issue = await github.get_issue("acme", "widget", 1)

    assert handler.state["issue_calls"] == 3  # type: ignore[attr-defined]
    assert issue.title == "ok"


async def test_get_retries_network_error_then_succeeds() -> None:
    """Transport-level errors (timeout, network) are retried."""
    github = GitHubClient(max_retries=2)
    handler = _issue_endpoint_handler(failure="timeout", fail_count=1)

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        issue = await github.get_issue("acme", "widget", 1)

    assert handler.state["issue_calls"] == 2  # type: ignore[attr-defined]
    assert issue.title == "ok"


async def test_get_raises_after_exhausting_retries_on_5xx() -> None:
    """When all retries are exhausted on 5xx, GitHubError is raised."""
    from app.github import GitHubError

    github = GitHubClient(max_retries=1)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "internal error"})

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        with pytest.raises(GitHubError, match="500"):
            await github.get_issue("acme", "widget", 1)


async def test_get_raises_after_exhausting_retries_on_network_error() -> None:
    """When all retries are exhausted on network errors, GitHubError is raised."""
    from app.github import GitHubError

    github = GitHubClient(max_retries=1)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        with pytest.raises(GitHubError, match="failed after"):
            await github.get_issue("acme", "widget", 1)


async def test_get_does_not_retry_4xx_client_errors() -> None:
    """4xx (non-rate-limit) errors are not retried."""
    from app.github import GitHubError

    github = GitHubClient(max_retries=3)
    call_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(404, json={"message": "not found"})

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        with pytest.raises(GitHubError, match="not found"):
            await github.get_issue("acme", "widget", 1)

    assert call_count == 1


async def test_get_issue_follows_repository_redirects() -> None:
    """GitHub returns 301 for case-mismatched owner/repo; client must follow."""
    github = GitHubClient(max_retries=0)

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # Any request to the lowercase owner/repo returns a 301 to canonical case.
        if "/repos/acme/widget" in url:
            canonical = url.replace("/repos/acme/widget", "/repos/Acme/Widget")
            return httpx.Response(301, headers={"Location": canonical})
        # Canonical endpoints: success.
        if "/comments" in url:
            return httpx.Response(200, json=[])
        if url.endswith("/repos/Acme/Widget") or url.endswith("/repos/Acme/Widget/"):
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(200, json={"title": "ok", "body": "", "labels": [], "state": "open"})

    await github._client.aclose()
    # follow_redirects must be enabled on the replacement client too, mirroring production config.
    github._client = httpx.AsyncClient(
        base_url="https://api.github.com",
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    async with github:
        issue = await github.get_issue("acme", "widget", 1)

    assert issue.title == "ok"
    assert issue.default_branch == "main"


async def test_get_issue_rejects_unexpected_comments_payload() -> None:
    """Non-list comments payload raises a readable GitHubError instead of crashing."""
    from app.github import GitHubError

    github = GitHubClient(max_retries=0)

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/comments" in url:
            # Simulate an upstream proxy returning a JSON error object instead of a list.
            return httpx.Response(200, json={"message": "Internal Server Error"})
        if url.endswith("/repos/acme/widget") or url.endswith("/repos/acme/widget/"):
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(200, json={"title": "ok", "body": "", "labels": [], "state": "open"})

    await github._client.aclose()
    github._client = httpx.AsyncClient(
        base_url="https://api.github.com",
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    async with github:
        with pytest.raises(GitHubError, match="Unexpected comments payload type"):
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


async def test_tree_and_file_cache_are_scoped_to_commit() -> None:
    issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
        head_sha="abc123",
    )
    calls = {"tree": 0, "file": 0}
    github = GitHubClient(cache_ttl=60)

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/git/trees/" in request.url.path:
            calls["tree"] += 1
            return httpx.Response(200, json={"tree": [{"path": "src/a.py", "type": "blob"}]})
        calls["file"] += 1
        return httpx.Response(200, json={"encoding": "base64", "content": "cHJpbnQoMSk="})

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        assert await github.get_tree(issue) == ["src/a.py"]
        assert await github.get_tree(issue) == ["src/a.py"]
        assert (await github.get_file(issue, "src/a.py")).content == "print(1)"
        assert (await github.get_file(issue, "src/a.py")).content == "print(1)"

    assert calls == {"tree": 1, "file": 1}
    assert github.cache_hits == 2
    assert github.cache_misses == 2


async def test_fork_shares_cache_but_keeps_request_metrics_local() -> None:
    issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
        head_sha="abc123",
    )
    github = GitHubClient(cache_ttl=60, close_on_exit=False)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tree": [{"path": "src/a.py", "type": "blob"}]})

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    first = github.fork()
    second = github.fork()

    assert await first.get_tree(issue) == ["src/a.py"]
    assert await second.get_tree(issue) == ["src/a.py"]

    assert first.cache_misses == 1
    assert first.cache_hits == 0
    assert second.cache_misses == 0
    assert second.cache_hits == 1
    assert github.cache_hits == github.cache_misses == 0
    await github.aclose()


async def test_cache_does_not_store_values_over_total_byte_limit() -> None:
    issue = IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
        head_sha="abc123",
    )
    request_count = 0
    github = GitHubClient(cache_ttl=60, cache_max_bytes=4)

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"encoding": "base64", "content": "cHJpbnQoMSk="})

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        await github.get_file(issue, "src/a.py")
        await github.get_file(issue, "src/a.py")

    assert request_count == 2
    assert not github._cache
    assert github._cache_state["bytes"] == 0


async def test_truncated_tree_falls_back_to_hierarchical_traversal() -> None:
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
    github = GitHubClient(max_tree_entries=10_000)
    requested_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        is_root = str(request.url).split("?", 1)[0].endswith("/git/trees/feature%2Ftest")
        if is_root and request.url.params.get("recursive") == "1":
            return httpx.Response(200, json={"truncated": True, "tree": []})
        if is_root:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "README.md", "type": "blob", "sha": "readme"},
                        {"path": "src", "type": "tree", "sha": "src-sha"},
                    ]
                },
            )
        if request.url.path.endswith("/git/trees/src-sha"):
            return httpx.Response(
                200,
                json={"tree": [{"path": "parser.py", "type": "blob", "sha": "parser"}]},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        paths = await github.get_tree(issue)

    assert paths == ["README.md", "src/parser.py"]
    assert any("feature%2Ftest" in url for url in requested_urls)
    assert all("%252F" not in url for url in requested_urls)


async def test_repository_code_search_scopes_query_and_returns_fragments() -> None:
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
    github = GitHubClient()
    requested_query = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested_query
        requested_query = request.url.params["q"]
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "path": "src/parser.py",
                        "text_matches": [{"fragment": "raise ParserError(message)"}],
                    }
                ]
            },
        )

    await github._client.aclose()
    github._client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with github:
        matches = await github.search_code(issue, "ParserError")

    assert requested_query == "ParserError repo:acme/widget"
    assert matches == [{"path": "src/parser.py", "fragments": ["raise ParserError(message)"]}]


async def test_repository_code_search_rejects_scope_override() -> None:
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
    github = GitHubClient()
    async with github:
        with pytest.raises(ValueError, match="scope"):
            await github.search_code(issue, "secret OR repo:another/private")


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
