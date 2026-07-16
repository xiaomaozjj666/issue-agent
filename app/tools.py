import json
import logging
import posixpath
import re

from app.config import Settings
from app.github import GitHubClient, GitHubFileSkipped
from app.models import IssueData

logger = logging.getLogger(__name__)

_MAX_LIST_ENTRIES = 80
_MAX_GREP_RESULTS = 50
_MAX_SEARCH_RESULTS = 50


_READ_ONLY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from the repository. Returns content with line "
                "numbers (L1: ...). Use start_line/end_line to read a slice."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path in the repository"},
                    "start_line": {"type": "integer", "description": "Start line (1-based), optional"},
                    "end_line": {"type": "integer", "description": "End line (1-based, inclusive), optional"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories under a given path in the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path, empty or '/' for root"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search file paths in the repository tree by keyword. Returns matching paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to search in file paths"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "Search code across the entire repository. Use this to find symbols or error messages "
                "that may not appear in filenames, then read matching files before citing them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Symbol, error text, or code fragment to find"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_content",
            "description": (
                "Search for a pattern in files that have already been read. "
                "Returns matching lines with file and line number."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_history",
            "description": (
                "Show recent git commits that touched a specific file. "
                "Returns commit SHA, author, date, and message. Useful for understanding "
                "when and why code was changed — like git blame."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path in the repository"},
                    "max_commits": {"type": "integer", "description": "Max commits to return (default 10)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_branches",
            "description": "List branches in the repository. Returns branch names and their latest commit SHA.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_at_commit",
            "description": (
                "Read a file's content at a specific git commit SHA. "
                "Use this to check how a file looked before recent changes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path in the repository"},
                    "sha": {"type": "string", "description": "Git commit SHA (7-40 hex chars)"},
                },
                "required": ["path", "sha"],
            },
        },
    },
]

_CREATE_PR_TOOL = {
    "type": "function",
    "function": {
        "name": "create_pull_request",
        "description": (
            "PROPOSE creating a pull request with code changes. "
            "This RETURNS A PROPOSAL ONLY — the PR is NOT created automatically. "
            "The user must review and confirm before anything is actually created. "
            "When called, clearly describe: branch name, files to modify, PR title and description."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Name for the new branch (e.g., fix/issue-42)"},
                "title": {"type": "string", "description": "PR title"},
                "body": {"type": "string", "description": "PR description explaining the changes"},
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path to modify"},
                            "content": {"type": "string", "description": "New file content"},
                            "message": {"type": "string", "description": "Commit message for this file"},
                        },
                        "required": ["path", "content", "message"],
                    },
                    "description": "List of file changes",
                },
            },
            "required": ["branch", "title", "body", "changes"],
        },
    },
}


def get_tool_definitions(settings: Settings) -> list[dict]:
    tools = list(_READ_ONLY_TOOLS)
    if settings.write_mode:
        tools.append(_CREATE_PR_TOOL)
    return tools


class ToolExecutor:
    def __init__(
        self,
        github: GitHubClient,
        settings: Settings,
        issue: IssueData,
        tree: list[str],
        *,
        file_cache: dict[str, str] | None = None,
        files_read: list[str] | None = None,
        max_files: int = 12,
        max_file_chars: int = 16_000,
        max_total_context_chars: int = 80_000,
    ) -> None:
        self._github = github
        self._settings = settings
        self._issue = issue
        self._tree = tree
        self._tree_set = set(tree)
        self._max_files = max_files
        self._max_file_chars = max_file_chars
        self._max_total_context_chars = max_total_context_chars
        self._tool_context_chars = 0
        self._file_cache: dict[str, str] = {}
        self.files_read: list[str] = []
        cached_paths = files_read if files_read is not None else list((file_cache or {}).keys())
        cached_chars = 0
        for path in cached_paths:
            content = (file_cache or {}).get(path)
            if content is None or path not in self._tree_set or len(self.files_read) >= max_files:
                continue
            remaining = max_total_context_chars - cached_chars
            if remaining <= 0:
                break
            self._file_cache[path] = content[:max_file_chars][:remaining]
            self.files_read.append(path)
            cached_chars += len(self._file_cache[path])
        self._cached_chars = cached_chars
        self._line_counts = {path: content.count("\n") + 1 for path, content in self._file_cache.items()}
        self.pr_proposal: dict | None = None
        self.tools_used: list[str] = []

    @property
    def file_cache(self) -> dict[str, str]:
        return self._file_cache

    async def execute(self, name: str, arguments: dict) -> str:
        self.tools_used.append(name)
        try:
            if name == "read_file":
                return await self._read_file(**arguments)
            if name == "list_directory":
                return self._list_directory(arguments.get("path", ""))
            if name == "search_files":
                return self._search_files(arguments["query"])
            if name == "search_code":
                return await self._search_code(arguments["query"])
            if name == "grep_content":
                return self._grep_content(arguments["pattern"])
            if name == "get_file_history":
                return await self._get_file_history(**arguments)
            if name == "list_branches":
                return await self._list_branches()
            if name == "get_file_at_commit":
                return await self._get_file_at_commit(**arguments)
            if name == "create_pull_request":
                return self._create_pull_request_proposal(**arguments)
            return f"Unknown tool: {name}"
        except GitHubFileSkipped as error:
            return f"File skipped: {error}"
        except Exception as error:
            logger.warning("Tool %s failed: %s", name, error)
            return f"Error: {error}"

    # ── core tools (unchanged) ──────────────────────────────────────

    async def _read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        path = self._normalize_file_path(path)
        full_content: str | None = None
        if path not in self._file_cache:
            if len(self.files_read) >= self._max_files:
                return f"File limit reached ({self._max_files}); use files already read."
            remaining = self._max_total_context_chars - self._cached_chars
            if remaining <= 0:
                return "Source context limit reached; use files already read."
            source = await self._github.get_file(self._issue, path)
            full_content = source.content
            self._file_cache[path] = source.content[: self._max_file_chars][:remaining]
            self.files_read.append(path)
            self._cached_chars += len(self._file_cache[path])
            self._line_counts[path] = source.content.count("\n") + 1

        content = self._file_cache[path]
        cached_line_count = content.count("\n") + 1
        if start_line is not None and (end_line is None or end_line > cached_line_count):
            if full_content is None:
                source = await self._github.get_file(self._issue, path)
                full_content = source.content
                self._line_counts[path] = source.content.count("\n") + 1
            content = full_content
        lines = content.splitlines()

        if start_line is not None and start_line < 1:
            start_line = 1
        if end_line is not None and end_line > len(lines):
            end_line = len(lines)

        if start_line is not None and end_line is not None:
            selected = lines[start_line - 1 : end_line]
            result = "\n".join(f"L{i}: {line}" for i, line in enumerate(selected, start_line))
            return self._limit_tool_context(result)
        if start_line is not None:
            selected = lines[start_line - 1 :]
            result = "\n".join(f"L{i}: {line}" for i, line in enumerate(selected, start_line))
            return self._limit_tool_context(result)
        result = "\n".join(f"L{i}: {line}" for i, line in enumerate(lines, 1))
        return self._limit_tool_context(result)

    def _normalize_file_path(self, path: str) -> str:
        if not isinstance(path, str) or not path or "\\" in path or path.startswith("/"):
            raise ValueError("Invalid repository file path")
        parts = path.split("/")
        if ".." in parts:
            raise ValueError("Invalid repository file path")
        normalized = posixpath.normpath(path)
        if normalized in {"", "."} or normalized not in self._tree_set:
            raise ValueError("File path is not present in the repository tree")
        return normalized

    def _limit_tool_context(self, result: str) -> str:
        remaining = self._max_total_context_chars - self._tool_context_chars
        if remaining <= 0:
            return "Source context limit reached; use files already read."
        limited = result[:remaining]
        self._tool_context_chars += len(limited)
        return limited

    def _list_directory(self, path: str) -> str:
        path = path.strip().strip("/")
        prefix = path + "/" if path else ""
        entries: set[str] = set()
        for p in self._tree:
            if not p.startswith(prefix):
                continue
            remainder = p[len(prefix) :]
            if not remainder:
                continue
            if "/" in remainder:
                entries.add(remainder.split("/", 1)[0] + "/")
            else:
                entries.add(remainder)
        if not entries:
            return f"Directory '{path or '/'}' is empty or does not exist"
        sorted_entries = sorted(entries)[:_MAX_LIST_ENTRIES]
        return "\n".join(sorted_entries)

    def _search_files(self, query: str) -> str:
        query_lower = query.lower()
        matches = [p for p in self._tree if query_lower in p.lower()]
        if not matches:
            return f"No files matching '{query}'"
        return "\n".join(matches[:_MAX_SEARCH_RESULTS])

    async def _search_code(self, query: str) -> str:
        matches = await self._github.search_code(self._issue, query, limit=_MAX_SEARCH_RESULTS)
        if not matches:
            return f"No repository code matches for '{query}'"
        lines: list[str] = []
        for match in matches:
            path = match["path"]
            fragments = match.get("fragments", [])
            lines.append(path)
            lines.extend(f"  {fragment}" for fragment in fragments)
        return self._limit_tool_context("\n".join(lines))

    def _grep_content(self, pattern: str) -> str:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)
        results: list[str] = []
        for path, content in self._file_cache.items():
            for i, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    results.append(f"{path}:L{i}: {line}")
                    if len(results) >= _MAX_GREP_RESULTS:
                        break
            if len(results) >= _MAX_GREP_RESULTS:
                break
        if not results:
            return f"No matches for '{pattern}' in files read so far. Use read_file first."
        return "\n".join(results)

    # ── new tools ────────────────────────────────────────────────────

    async def _get_file_history(self, path: str, max_commits: int = 10) -> str:
        path = self._normalize_file_path(path)
        commits = await self._github.get_file_history(self._issue, path, max_commits=max_commits)
        if not commits:
            return f"No commit history found for {path}"
        lines = [f"History for {path}:"]
        for c in commits:
            lines.append(f"  {c['sha']} {c['date']} {c['author']}: {c['message']}")
        return "\n".join(lines)

    async def _list_branches(self) -> str:
        branches = await self._github.list_branches(self._issue.owner, self._issue.repo)
        if not branches:
            return "No branches found"
        lines = ["Branches:"]
        for b in branches:
            protected = " [protected]" if b.get("protected") else ""
            lines.append(f"  {b['name']} ({b['sha']}){protected}")
        return "\n".join(lines)

    async def _get_file_at_commit(self, path: str, sha: str) -> str:
        path = self._normalize_file_path(path)
        sha = sha.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
            return "Error: SHA must be a 7-40 character hex string"
        source = await self._github.get_file_at_commit(self._issue, path, sha)
        lines = source.content.splitlines()
        result = "\n".join(f"L{i}: {line}" for i, line in enumerate(lines, 1))
        return self._limit_tool_context(result)

    def _create_pull_request_proposal(self, branch: str, title: str, body: str, changes: list[dict]) -> str:
        if not self._settings.write_mode:
            return "Error: Write mode is disabled. Set WRITE_MODE=true to enable PR creation."

        try:
            self.pr_proposal = validate_pr_proposal(
                self._settings,
                branch=branch,
                title=title,
                body=body,
                changes=changes,
                default_branch=self._issue.default_branch,
            )
        except ValueError as error:
            return f"Error: {error}"
        branch = self.pr_proposal["branch"]
        title = self.pr_proposal["title"]
        body = self.pr_proposal["body"]
        normalized_changes = self.pr_proposal["changes"]
        change_list = "\n".join(f"  - {c['path']}: {c['message']}" for c in normalized_changes)
        return (
            "⚠️  PR PROPOSAL — not yet created:\n\n"
            f"Branch: {branch}\n"
            f"Title: {title}\n"
            f"Description: {body}\n\n"
            f"Files to change:\n{change_list}\n\n"
            "---\n"
            "To create this PR, POST to /apply-fix with confirm=true"
        )

    @property
    def line_counts(self) -> dict[str, int]:
        counts = {path: content.count("\n") + 1 for path, content in self._file_cache.items()}
        counts.update(self._line_counts)
        return counts


def parse_tool_call(tool_call) -> tuple[str, dict]:
    name = tool_call.function.name
    try:
        arguments = json.loads(tool_call.function.arguments)
    except (json.JSONDecodeError, TypeError):
        arguments = {}
    return name, arguments


def validate_pr_proposal(
    settings: Settings,
    *,
    branch: str,
    title: str,
    body: str,
    changes: list[dict],
    default_branch: str | None = None,
) -> dict:
    normalized_branch = branch.strip().replace(" ", "-")
    if (
        not normalized_branch
        or len(normalized_branch) > 120
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", normalized_branch)
        or normalized_branch.startswith(("/", "."))
        or normalized_branch.endswith(("/", "."))
        or ".." in normalized_branch
    ):
        raise ValueError("Invalid branch name")
    if default_branch and normalized_branch == default_branch:
        raise ValueError("The proposal branch must differ from the repository default branch")
    normalized_title = title.strip()
    normalized_body = body.strip()
    if not normalized_title or not normalized_body:
        raise ValueError("PR title and body are required")
    if not changes or len(changes) > settings.max_pr_files:
        raise ValueError(f"A PR proposal must contain between 1 and {settings.max_pr_files} file changes")

    normalized_changes: list[dict] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for change in changes:
        path = str(change.get("path", ""))
        if not path or "\\" in path or path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"Invalid change path: {path or '<empty>'}")
        path = posixpath.normpath(path)
        content = change.get("content")
        message = str(change.get("message", "")).strip()
        if path in seen_paths or not isinstance(content, str) or not message:
            raise ValueError(f"Invalid or duplicate change for {path}")
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > settings.github_max_file_bytes:
            raise ValueError(f"Proposed content for {path} exceeds the file size limit")
        total_bytes += content_bytes
        if total_bytes > settings.max_pr_total_bytes:
            raise ValueError("Proposed changes exceed the total PR size limit")
        seen_paths.add(path)
        normalized_changes.append({"path": path, "content": content, "message": message})

    return {
        "branch": normalized_branch,
        "title": normalized_title,
        "body": normalized_body,
        "changes": normalized_changes,
    }
