import base64
import logging
import re
from urllib.parse import quote, urlparse

import httpx

from app.models import IssueData, SourceFile

logger = logging.getLogger(__name__)

ISSUE_PATH = re.compile(r"^/([^/]+)/([^/]+)/issues/(\d+)/?$")
SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".kt", ".php", ".py", ".rb", ".rs", ".scss",
    ".sh", ".sql", ".svelte", ".swift", ".ts", ".tsx", ".vue",
}
SKIP_PARTS = {
    ".git", ".github", ".idea", ".next", ".venv", "build", "coverage",
    "dist", "node_modules", "target", "vendor",
}
STOP_WORDS = {
    "the", "and", "that", "this", "with", "from", "when", "issue",
    "for", "are", "was", "were", "but", "not", "you", "your", "have",
    "has", "had", "all", "any", "can", "will", "would", "could", "should",
    "does", "did", "done", "into", "our", "their", "them", "they", "its",
    "about", "than", "then", "what", "which", "where", "while",
    "there", "here", "just", "like", "also", "very", "some", "such",
    "more", "most", "make", "makes", "made", "use", "using", "used",
    "get", "getting", "got", "via", "per", "out", "now", "still", "even",
    "bug", "feature", "request", "please", "help", "thanks", "thank",
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
    def __init__(self, token: str | None = None, max_file_bytes: int = 512_000) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-issue-agent",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com", headers=headers, timeout=30.0
        )
        self._max_file_bytes = max_file_bytes

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **kwargs: object) -> httpx.Response:
        response = await self._client.get(path, **kwargs)
        if response.status_code in RATE_LIMIT_STATUSES:
            retry_after = response.headers.get("Retry-After") or response.headers.get("X-RateLimit-Reset")
            detail = f"GitHub API rate limit hit (status {response.status_code})"
            if retry_after:
                detail += f"; retry hint: {retry_after}"
            raise GitHubRateLimitError(detail)
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = payload.get("message", response.text) if isinstance(payload, dict) else response.text
            except ValueError:
                message = response.text
            raise GitHubError(f"GitHub API returned {response.status_code}: {message}")
        return response

    @staticmethod
    def _repo_segment(owner: str, repo: str) -> str:
        return f"{quote(owner, safe='')}/{quote(repo, safe='')}"

    async def get_issue(self, owner: str, repo: str, number: int) -> IssueData:
        repo_segment = self._repo_segment(owner, repo)
        issue = (await self._get(f"/repos/{repo_segment}/issues/{number}")).json()
        if "pull_request" in issue:
            raise GitHubError("The supplied URL points to a pull request, not an issue")
        repository = (await self._get(f"/repos/{repo_segment}")).json()
        comments_response = await self._get(
            f"/repos/{repo_segment}/issues/{number}/comments", params={"per_page": 30}
        )
        comments = [item.get("body") or "" for item in comments_response.json()]
        link_header = comments_response.headers.get("Link", "")
        if "rel=\"next\"" in link_header:
            logger.info(
                "Issue %s/%s#%d has more than 30 comments; only first page fetched",
                owner, repo, number,
            )
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
            raise GitHubFileSkipped(
                f"File {path} is {size} bytes, exceeds limit {self._max_file_bytes}"
            )
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise GitHubFileSkipped(f"Unsupported file response for {path}")
        try:
            raw = base64.b64decode(
                "".join(data["content"].split()), validate=True
            )
        except (ValueError, base64.binascii.Error) as error:
            raise GitHubFileSkipped(f"Invalid file content for {path}") from error
        if b"\x00" in raw:
            raise GitHubFileSkipped(f"Binary file skipped: {path}")
        return SourceFile(path=path, content=raw.decode("utf-8", errors="replace"))


def select_candidate_paths(paths: list[str], issue: IssueData, limit: int) -> list[str]:
    text = " ".join([issue.title, issue.body, *issue.labels, *issue.comments]).lower()
    tokens = {
        token
        for token in re.findall(r"[a-zA-Z_][a-zA-Z0-9_.-]{2,}", text)
        if token not in STOP_WORDS
    }

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
