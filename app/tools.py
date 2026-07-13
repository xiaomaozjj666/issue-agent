import json
import logging
import posixpath
import re

from app.github import GitHubClient, GitHubFileSkipped
from app.models import IssueData

logger = logging.getLogger(__name__)

TOOL_DEFINITIONS = [
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
]

_MAX_LIST_ENTRIES = 80
_MAX_GREP_RESULTS = 50
_MAX_SEARCH_RESULTS = 50


class ToolExecutor:
    def __init__(
        self,
        github: GitHubClient,
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
            if name == "grep_content":
                return self._grep_content(arguments["pattern"])
            return f"Unknown tool: {name}"
        except GitHubFileSkipped as error:
            return f"File skipped: {error}"
        except Exception as error:
            logger.warning("Tool %s failed: %s", name, error)
            return f"Error: {error}"

    async def _read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        path = self._normalize_file_path(path)
        if path not in self._file_cache:
            if len(self.files_read) >= self._max_files:
                return f"File limit reached ({self._max_files}); use files already read."
            remaining = self._max_total_context_chars - self._cached_chars
            if remaining <= 0:
                return "Source context limit reached; use files already read."
            source = await self._github.get_file(self._issue, path)
            self._file_cache[path] = source.content[: self._max_file_chars][:remaining]
            self.files_read.append(path)
            self._cached_chars += len(self._file_cache[path])

        content = self._file_cache[path]
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

    @property
    def line_counts(self) -> dict[str, int]:
        return {path: content.count("\n") + 1 for path, content in self._file_cache.items()}


def parse_tool_call(tool_call) -> tuple[str, dict]:
    name = tool_call.function.name
    try:
        arguments = json.loads(tool_call.function.arguments)
    except (json.JSONDecodeError, TypeError):
        arguments = {}
    return name, arguments
