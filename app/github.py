"""GitHub REST API client with retry, connection pooling, and tree analysis.

Design decisions:
- Transient failures (5xx, network errors) are retried with exponential
  back-off; rate-limit responses (403/429) are surfaced immediately so
  callers can react.
- Connection pool limits prevent file-descriptor exhaustion under load.
- All file content is validated (base64, binary sniff) before returning.
"""

import asyncio
import base64
import binascii
import logging
import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.models import IssueData, SourceFile

logger = logging.getLogger(__name__)

ISSUE_PATH = re.compile(r"^/([^/]+)/([^/]+)/issues/(\d+)/?$")
SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".svelte",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}
SKIP_PARTS = {
    ".git",
    ".github",
    ".idea",
    ".next",
    ".venv",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
STOP_WORDS = {
    "the",
    "and",
    "that",
    "this",
    "with",
    "from",
    "when",
    "issue",
    "for",
    "are",
    "was",
    "were",
    "but",
    "not",
    "you",
    "your",
    "have",
    "has",
    "had",
    "all",
    "any",
    "can",
    "will",
    "would",
    "could",
    "should",
    "does",
    "did",
    "done",
    "into",
    "our",
    "their",
    "them",
    "they",
    "its",
    "about",
    "than",
    "then",
    "what",
    "which",
    "where",
    "while",
    "there",
    "here",
    "just",
    "like",
    "also",
    "very",
    "some",
    "such",
    "more",
    "most",
    "make",
    "makes",
    "made",
    "use",
    "using",
    "used",
    "get",
    "getting",
    "got",
    "via",
    "per",
    "out",
    "now",
    "still",
    "even",
    "bug",
    "feature",
    "request",
    "please",
    "help",
    "thanks",
    "thank",
}
RATE_LIMIT_STATUSES = {403, 429}


class GitHubError(RuntimeError):
    pass


class GitHubRateLimitError(GitHubError):
    pass


class GitHubFileSkipped(GitHubError):
    pass


def parse_issue_url(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError("issue_url must be an https://github.com URL")
    match = ISSUE_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("issue_url must match https://github.com/owner/repo/issues/123")
    return match.group(1), match.group(2), int(match.group(3))


class GitHubClient:
    """Async GitHub REST API client with retry and connection pooling.

    Usage::

        async with GitHubClient(token, timeout=30) as gh:
            issue = await gh.get_issue(owner, repo, number)
    """

    def __init__(
        self,
        token: str | None = None,
        max_file_bytes: int = 512_000,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        write_enabled: bool = False,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-issue-agent",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=headers,
            timeout=timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            # GitHub API returns 301 for case-mismatched owner/repo (e.g.
            # httpie/httpie -> HTTPie/httpie). Follow redirects so callers
            # always see the canonical 200 response, not a 3xx body that
            # would break .json() parsing.
            follow_redirects=True,
        )
        self._max_file_bytes = max_file_bytes
        self._max_retries = max_retries
        self._write_enabled = write_enabled

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        """Perform a GET request with exponential back-off for transient errors.

        Rate-limit responses (403/429) raise immediately without retry.
        Server errors (5xx) and transport failures (timeouts, network errors,
        remote protocol errors) are retried up to ``self._max_retries`` times
        with exponential delay.
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(path, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    delay = 0.5 * (2**attempt)
                    logger.warning(
                        "GitHub request %s failed (attempt %d): %s; retrying in %.1fs",
                        path,
                        attempt + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise GitHubError(f"GitHub request failed after {self._max_retries + 1} attempts: {exc}") from exc

            if response.status_code in RATE_LIMIT_STATUSES:
                retry_after = response.headers.get("Retry-After") or response.headers.get("X-RateLimit-Reset")
                detail = f"GitHub API rate limit hit (status {response.status_code})"
                if retry_after:
                    detail += f"; retry hint: {retry_after}"
                raise GitHubRateLimitError(detail)

            if response.status_code >= 500 and attempt < self._max_retries:
                delay = 0.5 * (2**attempt)
                logger.warning(
                    "GitHub %s returned %d (attempt %d); retrying in %.1fs",
                    path,
                    response.status_code,
                    attempt + 1,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                try:
                    payload = response.json()
                    message = payload.get("message", response.text) if isinstance(payload, dict) else response.text
                except ValueError:
                    message = response.text
                raise GitHubError(f"GitHub API returned {response.status_code}: {message}")
            return response

        # Should not reach here, but satisfy type checker
        raise GitHubError(f"GitHub request failed: {last_exc}")  # pragma: no cover

    @staticmethod
    def _repo_segment(owner: str, repo: str) -> str:
        return f"{quote(owner, safe='')}/{quote(repo, safe='')}"

    async def get_issue(self, owner: str, repo: str, number: int) -> IssueData:
        repo_segment = self._repo_segment(owner, repo)
        issue = (await self._get(f"/repos/{repo_segment}/issues/{number}")).json()
        if "pull_request" in issue:
            raise GitHubError(
                f"The supplied URL (https://github.com/{owner}/{repo}/issues/{number}) "
                f"points to a pull request, not an issue. "
                f"Use https://github.com/{owner}/{repo}/pull/{number} to view the PR, "
                f"or pick an issue from https://github.com/{owner}/{repo}/issues."
            )
        repository = (await self._get(f"/repos/{repo_segment}")).json()
        comments_response = await self._get(f"/repos/{repo_segment}/issues/{number}/comments", params={"per_page": 30})
        comments_payload = comments_response.json()
        # 防御：GitHub API 文档保证此端点返回 list，但若上游代理/缓存返回
        # 非预期结构（例如 dict 错误对象），迭代 dict 会得到字符串 key
        # 并在后续 .get() 调用上崩溃。显式校验以给出可读错误。
        if not isinstance(comments_payload, list):
            raise GitHubError(
                f"Unexpected comments payload type {type(comments_payload).__name__} for "
                f"{owner}/{repo}#{number}; expected list"
            )
        comments = [item.get("body") or "" for item in comments_payload if isinstance(item, dict)]
        link_header = comments_response.headers.get("Link", "")
        if 'rel="next"' in link_header:
            logger.info("Issue %s/%s#%d has more than 30 comments; only first page fetched", owner, repo, number)
        return IssueData(
            owner=owner,
            repo=repo,
            number=number,
            title=issue["title"],
            body=issue.get("body") or "",
            labels=[label["name"] for label in issue.get("labels", [])],
            comments=comments,
            default_branch=repository["default_branch"],
        )

    async def get_tree(self, issue: IssueData) -> list[str]:
        branch = quote(issue.default_branch, safe="")
        repo_segment = self._repo_segment(issue.owner, issue.repo)
        try:
            response = await self._get(
                f"/repos/{repo_segment}/git/trees/{branch}",
                params={"recursive": "1"},
            )
        except GitHubError as error:
            if "409" in str(error):
                raise GitHubError("Repository is empty, no tree available for analysis") from error
            raise
        tree = response.json()
        if tree.get("truncated"):
            raise GitHubError("Repository tree is too large for safe analysis")
        return [item["path"] for item in tree["tree"] if item.get("type") == "blob"]

    async def get_file(self, issue: IssueData, path: str) -> SourceFile:
        encoded_path = quote(path, safe="/")
        repo_segment = self._repo_segment(issue.owner, issue.repo)
        response = await self._get(
            f"/repos/{repo_segment}/contents/{encoded_path}",
            params={"ref": issue.default_branch},
        )
        data = response.json()
        size = data.get("size")
        if isinstance(size, int) and size > self._max_file_bytes:
            raise GitHubFileSkipped(f"File {path} is {size} bytes, exceeds limit {self._max_file_bytes}")
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise GitHubFileSkipped(f"Unsupported file response for {path}")
        try:
            raw = base64.b64decode("".join(data["content"].split()), validate=True)
        except (ValueError, binascii.Error) as error:
            raise GitHubFileSkipped(f"Invalid file content for {path}") from error
        if b"\x00" in raw:
            raise GitHubFileSkipped(f"Binary file skipped: {path}")
        return SourceFile(path=path, content=raw.decode("utf-8", errors="replace"))

    # ── v0.3.0 extended API ─────────────────────────────────────────

    async def get_file_history(self, issue: IssueData, path: str, max_commits: int = 10) -> list[dict]:
        repo_segment = self._repo_segment(issue.owner, issue.repo)
        response = await self._get(
            f"/repos/{repo_segment}/commits",
            params={"path": path, "per_page": min(max_commits, 30), "sha": issue.default_branch},
        )
        commits = response.json()
        if not isinstance(commits, list):
            return []
        return [
            {
                "sha": c.get("sha", "")[:7],
                "author": (c.get("commit", {}).get("author", {}).get("name", "unknown")),
                "date": (c.get("commit", {}).get("author", {}).get("date", "")[:10]),
                "message": (c.get("commit", {}).get("message", "").split("\n")[0][:120]),
            }
            for c in commits
        ]

    async def search_code(self, issue: IssueData, query: str, limit: int = 20) -> list[dict]:
        """Search repository-wide code without requiring the agent to read every candidate file."""
        normalized = " ".join(query.split()).strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("Code search query must contain between 1 and 120 characters")
        if re.search(r"\b(?:repo|org|user|fork):", normalized, re.IGNORECASE):
            raise ValueError("Code search query cannot override repository scope")
        repo_filter = f"repo:{issue.owner}/{issue.repo}"
        response = await self._get(
            "/search/code",
            params={"q": f"{normalized} {repo_filter}", "per_page": max(1, min(limit, 30))},
            headers={"Accept": "application/vnd.github.text-match+json"},
        )
        payload = response.json()
        items = payload.get("items", []) if isinstance(payload, dict) else []
        results: list[dict] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            fragments = [
                match.get("fragment", "")[:500]
                for match in item.get("text_matches", [])
                if isinstance(match, dict) and match.get("fragment")
            ]
            results.append({"path": item["path"], "fragments": fragments[:3]})
        return results

    async def list_branches(self, owner: str, repo: str) -> list[dict]:
        repo_segment = self._repo_segment(owner, repo)
        response = await self._get(f"/repos/{repo_segment}/branches", params={"per_page": 30})
        branches = response.json()
        if not isinstance(branches, list):
            return []
        return [
            {
                "name": b.get("name", ""),
                "sha": (b.get("commit", {}).get("sha", "")[:7]),
                "protected": b.get("protected", False),
            }
            for b in branches
        ]

    async def get_file_at_commit(self, issue: IssueData, path: str, sha: str) -> SourceFile:
        encoded_path = quote(path, safe="/")
        repo_segment = self._repo_segment(issue.owner, issue.repo)
        response = await self._get(
            f"/repos/{repo_segment}/contents/{encoded_path}",
            params={"ref": sha},
        )
        data = response.json()
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise GitHubFileSkipped(f"Unsupported file response for {path} at {sha}")
        try:
            raw = base64.b64decode("".join(data["content"].split()), validate=True)
        except (ValueError, binascii.Error) as error:
            raise GitHubFileSkipped(f"Invalid file content for {path}") from error
        if b"\x00" in raw:
            raise GitHubFileSkipped(f"Binary file skipped: {path}")
        return SourceFile(path=path, content=raw.decode("utf-8", errors="replace"))

    def _check_write_mode(self) -> None:
        if not self._write_enabled:
            raise GitHubError("Write mode is disabled. Set WRITE_MODE=true to enable.")

    async def get_branch_sha(self, owner: str, repo: str, branch: str) -> str:
        repo_segment = self._repo_segment(owner, repo)
        encoded_branch = quote(branch, safe="")
        data = (await self._get(f"/repos/{repo_segment}/branches/{encoded_branch}")).json()
        sha = data.get("commit", {}).get("sha", "")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise GitHubError(f"GitHub returned an invalid SHA for branch {branch}")
        return sha

    async def get_file_sha(self, owner: str, repo: str, path: str, ref: str) -> str | None:
        repo_segment = self._repo_segment(owner, repo)
        encoded_path = quote(path, safe="/")
        response = await self._client.get(
            f"/repos/{repo_segment}/contents/{encoded_path}",
            params={"ref": ref},
        )
        if response.status_code == 404:
            return None
        if response.status_code in RATE_LIMIT_STATUSES:
            raise GitHubRateLimitError(f"GitHub API rate limit hit (status {response.status_code})")
        if response.status_code >= 400:
            raise GitHubError(f"Failed to inspect existing file: {response.text}")
        sha = response.json().get("sha")
        return sha if isinstance(sha, str) and sha else None

    async def create_branch(self, owner: str, repo: str, branch: str, base_sha: str) -> dict:
        self._check_write_mode()
        repo_segment = self._repo_segment(owner, repo)
        response = await self._client.post(
            f"/repos/{repo_segment}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        if response.status_code >= 400:
            raise GitHubError(f"Failed to create branch: {response.text}")
        return response.json()

    async def delete_branch(self, owner: str, repo: str, branch: str) -> None:
        """Best-effort rollback for a branch created by an incomplete PR workflow."""
        self._check_write_mode()
        repo_segment = self._repo_segment(owner, repo)
        encoded_branch = quote(branch, safe="")
        response = await self._client.delete(f"/repos/{repo_segment}/git/refs/heads/{encoded_branch}")
        if response.status_code not in {204, 404}:
            raise GitHubError(f"Failed to roll back branch: {response.text}")

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        branch: str,
        message: str,
    ) -> dict:
        self._check_write_mode()
        repo_segment = self._repo_segment(owner, repo)
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        current_sha = await self.get_file_sha(owner, repo, path, branch)
        if current_sha is not None:
            payload["sha"] = current_sha
        response = await self._client.put(
            f"/repos/{repo_segment}/contents/{quote(path, safe='/')}",
            json=payload,
        )
        if response.status_code >= 400:
            raise GitHubError(f"Failed to write file: {response.text}")
        return response.json()

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict:
        self._check_write_mode()
        repo_segment = self._repo_segment(owner, repo)
        response = await self._client.post(
            f"/repos/{repo_segment}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        if response.status_code >= 400:
            raise GitHubError(f"Failed to create PR: {response.text}")
        data = response.json()
        pr_url = data.get("html_url", "")
        if not isinstance(pr_url, str) or not pr_url.startswith("https://github.com/"):
            raise GitHubError("GitHub returned an invalid pull request URL")
        return {"pr_url": pr_url, "number": data.get("number", 0)}


def select_candidate_paths(paths: list[str], issue: IssueData, limit: int) -> list[str]:
    text = " ".join([issue.title, issue.body, *issue.labels, *issue.comments]).lower()
    tokens = {token for token in re.findall(r"[a-zA-Z_][a-zA-Z0-9_.-]{2,}", text) if token not in STOP_WORDS}
    scored: list[tuple[int, str]] = []
    for path in paths:
        lowered = path.lower()
        parts = set(lowered.split("/"))
        suffix = "." + lowered.rsplit(".", 1)[-1] if "." in lowered else ""
        if suffix not in SOURCE_EXTENSIONS or parts & SKIP_PARTS:
            continue
        path_tokens = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", lowered))
        score = sum(6 for token in tokens if token in path_tokens)
        score += sum(3 for token in tokens if token not in path_tokens and token in lowered)
        if any(name in lowered for name in ("test", "spec")):
            score += 1
        if lowered.startswith(("src/", "app/", "lib/", "packages/")):
            score += 2
        scored.append((score, path))
    scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    return [path for _, path in scored[:limit]]


# 匹配 issue 文本中显式提到的文件路径，如 src/requests/models.py、requests/models.py、models.py。
# 必须包含扩展名，避免误匹配普通单词。前导边界用 [^\w/.-] 排除 URL 片段或变量名。
_FILE_REF_PATTERN = re.compile(
    r"(?:^|[^\w/.-])"
    r"((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_-]+\.[A-Za-z]\w{1,10}"
    r"|[A-Za-z0-9_-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|c|cc|cpp|h|hpp|cs|rb|php|kt|swift|sql|sh|css|scss|html|svelte|vue))"
)


def extract_referenced_paths(text: str, tree: list[str]) -> list[str]:
    """Extract file paths mentioned in `text` that actually exist in `tree`.

    Returns paths in first-mention order, deduplicated. Used to force-read
    source files the issue text explicitly cites (e.g. `src/requests/models.py`
    in a bug report) so the agent never produces a speculative root cause.
    """
    if not text or not tree:
        return []
    tree_set = set(tree)
    seen: set[str] = set()
    result: list[str] = []
    for match in _FILE_REF_PATTERN.finditer(text):
        candidate = match.group(1).strip(".")
        if candidate in tree_set and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result
