"""Tool definitions and execution engine for the investigation agent.

The ``ToolExecutor`` class provides a sandboxed environment for LLM tool calls:
- File reads are bounded by max_files, max_file_chars, and total context budget.
- An investigation ledger records bounded observations for report generation.
- PR proposals are validated but never executed directly (opt-in confirmation).

Tool dispatch uses a method-name convention (``_tool_<name>``) resolved via
``getattr``, making it trivial to register new tools without modifying a
central if/elif chain.
"""

import asyncio
import json
import logging
import posixpath
import re
from collections.abc import Callable, Coroutine
from typing import Any

from app.config import Settings
from app.github import GitHubClient, GitHubFileSkipped
from app.models import IssueData
from app.source_context import SourceSample, iter_source_lines, render_numbered_source, sample_source

logger = logging.getLogger(__name__)

_MAX_LIST_ENTRIES = 80
_MAX_GREP_RESULTS = 50
_MAX_REGEX_PATTERN_CHARS = 256
_GREP_TIMEOUT_SECONDS = 1.0
# 嵌套量词启发式（如 (a+)+ / (a*)* / (a+){2,}）：经典灾难性回溯形态，直接拒绝执行
_DANGEROUS_REGEX = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*[+*{]")
_MAX_SEARCH_RESULTS = 50
_FOCUS_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{3,}")
_DUPLICATE_TOOL_RESULT = "Duplicate tool call skipped; the unchanged result is already present in the conversation."
_PARALLEL_SAFE_TOOLS = {
    "read_file",
    "list_directory",
    "search_files",
    "search_code",
    "get_file_history",
    "list_branches",
    "get_file_at_commit",
}

# ── 写路径保护（模型生成的补丁绝不能改这些）──────────────────
# CI 工作流可被 pull_request_target 触发任意代码执行；容器/依赖/密钥文件同理。
_PROTECTED_PATH_PREFIXES = (".github/", ".git/", ".circleci/", ".gitlab/", ".husky/")
_PROTECTED_PATH_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".lock")
_PROTECTED_PATH_NAMES = {
    "dockerfile",
    ".env",
    ".env.local",
    ".env.production",
    "codeowners",
    "package-lock.json",
    "uv.lock",
    "poetry.lock",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pipfile.lock",
}
# 人工/发布分支不允许作为提案的目标分支
_PROTECTED_BRANCHES = {"main", "master", "develop", "development", "trunk", "gh-pages"}
_PROTECTED_BRANCH_PREFIXES = ("release/", "hotfix/", "rc/")


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
                "Returns matching lines with file and line number. "
                "Optionally restrict the search to a single path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {
                        "type": "string",
                        "description": "Optional: restrict search to this single already-read file path",
                    },
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


def _regex_body_has(pattern: str, target: str) -> bool:
    """Find an unescaped character outside character classes."""
    escaped = False
    in_class = False
    for char in pattern:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif char == "[" and not in_class:
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        elif char == target and not in_class:
            return True
    return False


def _regex_has_quantifier(pattern: str) -> bool:
    index = 0
    in_class = False
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if char == "[" and not in_class:
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        elif (
            not in_class
            and char in "+*"
            or not in_class
            and char == "?"
            and (index == 0 or pattern[index - 1] != "(")
            or not in_class
            and char == "{"
            and re.match(r"\{\d+(?:,\d*)?\}", pattern[index:])
        ):
            return True
        index += 1
    return False


def _regex_refusal_reason(pattern: str) -> str | None:
    """Reject catastrophic-backtracking shapes before touching CPython's engine."""
    if len(pattern) > _MAX_REGEX_PATTERN_CHARS:
        return f"patterns longer than {_MAX_REGEX_PATTERN_CHARS} characters"
    if _DANGEROUS_REGEX.search(pattern):
        return "nested quantifiers"

    stack: list[int] = []
    index = 0
    in_class = False
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if char == "[" and not in_class:
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        elif char == "(" and not in_class:
            stack.append(index + 1)
        elif char == ")" and not in_class and stack:
            body_start = stack.pop()
            next_char = pattern[index + 1 : index + 2]
            repeated = next_char in {"+", "*"} or (
                next_char == "{" and re.match(r"\{\d+(?:,\d*)?\}", pattern[index + 1 :]) is not None
            )
            if repeated:
                body = pattern[body_start:index]
                if _regex_body_has(body, "|"):
                    return "repeated alternation with ambiguous branches"
                if _regex_has_quantifier(body):
                    return "nested quantifiers"
        index += 1
    return None


class ToolExecutor:
    """Sandboxed tool execution environment for the investigation agent.

    Enforces file-count, character-budget, and context-window limits.
    Maintains an investigation ledger (bounded observation log) consumed
    by the report generator, and a file cache shared across tool calls.
    """

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
        self._ledger_chars = 0
        self._investigation_ledger: list[str] = []
        self._file_cache: dict[str, str] = {}
        self._source_documents: dict[str, str] = {}
        self._source_samples: dict[str, SourceSample] = {}
        self._source_focus: dict[str, list[tuple[int, int]]] = {}
        self._source_budgets: dict[str, int] = {}
        self._grep_lock = asyncio.Lock()
        self._grep_timed_out = False
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
        self._agent_result_cache: dict[str, str] = {}

    @property
    def file_cache(self) -> dict[str, str]:
        return self._file_cache

    @property
    def source_coverage(self) -> list[dict]:
        """Disclose only files whose original content was actually truncated."""
        return [
            sample.coverage(path)
            for path, sample in self._source_samples.items()
            if len(sample.text) < sample.original_chars
        ]

    async def execute(self, name: str, arguments: dict) -> str:
        """Dispatch and execute a tool call by name.

        Uses method-name convention: tool ``foo_bar`` maps to ``_tool_foo_bar``,
        resolved via ``getattr`` so new tools can be added without touching a
        central dispatch chain.
        """
        self.tools_used.append(name)
        handler: Callable[..., Coroutine[Any, Any, str]] | None = getattr(self, f"_tool_{name}", None)
        if handler is None:
            result = f"Unknown tool: {name}"
            self._record_observation(name, arguments, result)
            return result
        try:
            result = await handler(**arguments)
        except GitHubFileSkipped as error:
            result = f"File skipped: {error}"
        except Exception as error:
            logger.warning("Tool %s failed: %s", name, error)
            result = f"Error: {error}"
        self._record_observation(name, arguments, result)
        return result

    async def execute_many(self, calls: list[tuple[str, dict]], *, timeout: float) -> tuple[list[str], int]:
        """Execute a model tool-call batch while preserving response order.

        Independent read-only calls run concurrently. Exact repeats from an
        earlier model turn, or duplicates within the same batch, are replaced
        with a short result so the LLM context does not grow with identical
        source excerpts. Batches containing cache-dependent or write tools stay
        sequential to preserve tool-call ordering semantics.
        """
        if not calls:
            return [], 0

        results: list[str | None] = [None] * len(calls)
        pending: list[tuple[int, str, dict, str]] = []
        seen_in_batch: set[str] = set()
        duplicate_count = 0
        for index, (name, arguments) in enumerate(calls):
            key = _tool_call_key(name, arguments)
            if key in self._agent_result_cache or key in seen_in_batch:
                results[index] = _DUPLICATE_TOOL_RESULT
                duplicate_count += 1
                continue
            seen_in_batch.add(key)
            pending.append((index, name, arguments, key))

        semaphore = asyncio.Semaphore(self._settings.max_parallel_tool_calls)

        async def run(name: str, arguments: dict) -> str:
            async with semaphore:
                try:
                    return await asyncio.wait_for(self.execute(name, arguments), timeout=timeout)
                except TimeoutError:
                    logger.warning("Tool %s timed out after %ss", name, timeout)
                    return f"Error: Tool '{name}' timed out after {timeout:.0f}s"

        if pending and all(name in _PARALLEL_SAFE_TOOLS for _, name, _, _ in pending):
            pending_results = await asyncio.gather(*(run(name, arguments) for _, name, arguments, _ in pending))
        else:
            pending_results = [await run(name, arguments) for _, name, arguments, _ in pending]

        for (index, _name, _arguments, key), result in zip(pending, pending_results, strict=True):
            results[index] = result
            if not result.startswith(("Error:", "File skipped:")):
                self._agent_result_cache[key] = result
        return [result or "Error: Tool returned no result" for result in results], duplicate_count

    @property
    def investigation_ledger(self) -> list[str]:
        return list(self._investigation_ledger)

    def _record_observation(self, name: str, arguments: dict, result: str) -> None:
        remaining = self._settings.max_investigation_ledger_chars - self._ledger_chars
        if remaining <= 0:
            return
        safe_arguments = arguments
        if name == "create_pull_request":
            safe_arguments = {
                "branch": arguments.get("branch"),
                "title": arguments.get("title"),
                "changes": [
                    {"path": change.get("path"), "message": change.get("message")}
                    for change in arguments.get("changes", [])
                    if isinstance(change, dict)
                ],
            }
        rendered_arguments = json.dumps(safe_arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if name == "read_file" and not result.startswith(("Error:", "File skipped:")):
            observation = f"{name}({rendered_arguments}) -> source captured ({len(result)} characters returned)"
        else:
            observation = f"{name}({rendered_arguments}) -> {result}"
        limited = observation[: min(remaining, 2_500)]
        self._investigation_ledger.append(limited)
        self._ledger_chars += len(limited)

    # ── core tools ──────────────────────────────────────────────────

    async def _tool_read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        path = self._normalize_file_path(path)
        full_content = self._source_documents.get(path)
        fetched_in_call = False
        if path not in self._file_cache:
            if len(self.files_read) >= self._max_files:
                return f"File limit reached ({self._max_files}); use files already read."
            remaining = self._max_total_context_chars - self._cached_chars
            if remaining <= 0:
                return "Source context limit reached; use files already read."
            source = await self._github.get_file(self._issue, path)
            fetched_in_call = True
            # 并发预读场景下，await 期间其它协程可能已消耗预算，必须重新校验。
            # asyncio 单线程模型保证此同步块原子执行，不会再次被协程切换打断。
            full_content = source.content
            if path not in self._file_cache:
                if len(self.files_read) >= self._max_files:
                    return f"File limit reached ({self._max_files}); use files already read."
                remaining = self._max_total_context_chars - self._cached_chars
                if remaining <= 0:
                    return "Source context limit reached; use files already read."
                budget = min(self._max_file_chars, remaining)
                focus = self._issue_focus_ranges(full_content)
                sample = sample_source(full_content, budget, focus)
                self._source_documents[path] = full_content
                self._source_samples[path] = sample
                self._source_focus[path] = focus
                self._source_budgets[path] = budget
                self._file_cache[path] = sample.text
                self.files_read.append(path)
                self._cached_chars += len(sample.text)
                self._line_counts[path] = full_content.count("\n") + 1

        if start_line is not None:
            start_line = max(1, start_line)
        if start_line is not None or end_line is not None:
            # 显式行范围总是从原始文档读取，并把该区段提升到最终报告缓存。
            # 这不仅支持尾部引用，也避免 omission marker 被误当成源码行。
            range_start = start_line or 1
            range_end = end_line if end_line is not None else self._line_counts[path]
            if full_content is not None and not fetched_in_call and start_line is not None:
                cached_line_count = self._file_cache[path].count("\n") + 1
                available_lines = {
                    line_number
                    for line_number, _line in iter_source_lines(self._file_cache[path])
                    if line_number is not None
                }
                range_missing = any(
                    line_number not in available_lines
                    for line_number in range(range_start, min(range_end, self._line_counts[path]) + 1)
                )
                # 保留原有缓存契约：start-only 或超出旧缓存范围时重新拉取，
                # 防止远程分支已变化时持续返回进程内的陈旧原文。
                if end_line is None or end_line > cached_line_count or range_missing:
                    full_content = None
            if full_content is None:
                source = await self._github.get_file(self._issue, path)
                full_content = source.content
                self._source_documents[path] = full_content
                self._line_counts[path] = full_content.count("\n") + 1
            if path not in self._source_budgets:
                remaining = self._max_total_context_chars - self._cached_chars
                self._source_budgets[path] = min(
                    self._max_file_chars,
                    len(self._file_cache[path]) + max(remaining, 0),
                )
                self._source_focus[path] = []
            self._source_focus[path].append((range_start, max(range_start, range_end)))
            self._refresh_source_sample(path)
            lines = full_content.splitlines()
            selected = lines[range_start - 1 : range_end]
            result = "\n".join(f"L{i}: {line}" for i, line in enumerate(selected, range_start))
            return self._limit_tool_context(result)

        result = render_numbered_source(self._file_cache[path])
        return self._limit_tool_context(result)

    def _issue_focus_ranges(self, content: str) -> list[tuple[int, int]]:
        """Promote source lines related to issue identifiers, with local context."""
        issue_text = " ".join([self._issue.title, self._issue.body, *self._issue.comments])
        tokens = {match.group(0).lower() for match in _FOCUS_TOKEN_RE.finditer(issue_text)}
        if not tokens:
            return []
        ranges: list[tuple[int, int]] = []
        for line_number, line in enumerate(content.splitlines(), 1):
            lowered = line.lower()
            if any(token in lowered for token in tokens):
                ranges.append((max(1, line_number - 3), line_number + 3))
                if len(ranges) >= 8:
                    break
        return ranges

    def _refresh_source_sample(self, path: str) -> None:
        """Rebuild one cached excerpt without exceeding its reserved/total budget."""
        full_content = self._source_documents[path]
        old_length = len(self._file_cache.get(path, ""))
        remaining = self._max_total_context_chars - self._cached_chars
        budget = min(self._source_budgets[path], old_length + max(remaining, 0))
        sample = sample_source(full_content, budget, self._source_focus.get(path, []))
        self._file_cache[path] = sample.text
        self._source_samples[path] = sample
        self._cached_chars += len(sample.text) - old_length

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

    async def _tool_list_directory(self, path: str = "") -> str:
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

    async def _tool_search_files(self, query: str) -> str:
        query_lower = query.lower()
        matches = [p for p in self._tree if query_lower in p.lower()]
        if not matches:
            return f"No files matching '{query}'"
        return "\n".join(matches[:_MAX_SEARCH_RESULTS])

    async def _tool_search_code(self, query: str) -> str:
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

    def _grep_lines(self, regex: re.Pattern[str], cache: dict[str, str]) -> tuple[list[str], dict[str, list[int]]]:
        """Synchronous scan executed in a worker thread with original line numbers."""
        results: list[str] = []
        matches: dict[str, list[int]] = {}
        for file_path, content in cache.items():
            for line_number, line in iter_source_lines(content):
                if line_number is not None and regex.search(line):
                    results.append(f"{file_path}:L{line_number}: {line}")
                    matches.setdefault(file_path, []).append(line_number)
                    if len(results) >= _MAX_GREP_RESULTS:
                        return results, matches
        return results, matches

    async def _tool_grep_content(self, pattern: str, path: str | None = None) -> str:
        # CPython re 没有匹配超时：先做结构复杂度拒绝，再在工作线程执行并设置
        # wall-clock 上限。一次超时后禁用本执行器的后续 grep，避免恶意调用不断
        # 制造无法被杀死的后台线程；事件循环和 SSE 心跳始终保持可取消。
        refusal = _regex_refusal_reason(pattern)
        if refusal:
            return f"Refusing to run this pattern: {refusal} can trigger catastrophic backtracking."
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)
        # 可选 path 过滤：搜索原始文档而非仅搜索采样，命中区段随后进入报告缓存。
        cache = {key: self._source_documents.get(key, content) for key, content in self._file_cache.items()}
        if path is not None:
            normalized = self._normalize_file_path(path)
            if normalized not in cache:
                return f"'{path}' has not been read yet. Use read_file first, or omit path to search all read files."
            cache = {normalized: cache[normalized]}
        async with self._grep_lock:
            if self._grep_timed_out:
                return "Search unavailable: a previous regex timed out; use literal read_file ranges instead."
            try:
                results, matches = await asyncio.wait_for(
                    asyncio.to_thread(self._grep_lines, regex, cache),
                    timeout=_GREP_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                self._grep_timed_out = True
                logger.warning(
                    "grep_content timed out after %ss; further regex searches disabled", _GREP_TIMEOUT_SECONDS
                )
                return "Search timed out; further regex searches are disabled for this investigation."
        for file_path, line_numbers in matches.items():
            if file_path not in self._source_documents:
                continue
            self._source_focus[file_path].extend(
                (max(1, line_number - 3), line_number + 3) for line_number in line_numbers[:4]
            )
            self._refresh_source_sample(file_path)
        if not results:
            scope = f"'{path}'" if path else "files read so far"
            return f"No matches for '{pattern}' in {scope}."
        return "\n".join(results)

    # ── extended tools ──────────────────────────────────────────────

    async def _tool_get_file_history(self, path: str, max_commits: int = 10) -> str:
        path = self._normalize_file_path(path)
        commits = await self._github.get_file_history(self._issue, path, max_commits=max_commits)
        if not commits:
            return f"No commit history found for {path}"
        lines = [f"History for {path}:"]
        for c in commits:
            lines.append(f"  {c['sha']} {c['date']} {c['author']}: {c['message']}")
        return "\n".join(lines)

    async def _tool_list_branches(self) -> str:
        branches = await self._github.list_branches(self._issue.owner, self._issue.repo)
        if not branches:
            return "No branches found"
        lines = ["Branches:"]
        for b in branches:
            protected = " [protected]" if b.get("protected") else ""
            lines.append(f"  {b['name']} ({b['sha']}){protected}")
        return "\n".join(lines)

    async def _tool_get_file_at_commit(self, path: str, sha: str) -> str:
        path = self._normalize_file_path(path)
        sha = sha.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
            return "Error: SHA must be a 7-40 character hex string"
        source = await self._github.get_file_at_commit(self._issue, path, sha)
        lines = source.content.splitlines()
        result = "\n".join(f"L{i}: {line}" for i, line in enumerate(lines, 1))
        return self._limit_tool_context(result)

    async def _tool_create_pull_request(self, branch: str, title: str, body: str, changes: list[dict]) -> str:
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
            "To create this PR, use the session's apply-fix action and explicitly confirm the write."
        )

    @property
    def line_counts(self) -> dict[str, int]:
        counts = {path: content.count("\n") + 1 for path, content in self._file_cache.items()}
        counts.update(self._line_counts)
        return counts


def parse_tool_call(tool_call: Any) -> tuple[str, dict]:
    """Extract tool name and parsed arguments from an OpenAI tool_call object."""
    name = tool_call.function.name
    try:
        arguments = json.loads(tool_call.function.arguments)
    except (json.JSONDecodeError, TypeError):
        arguments = {}
    return name, arguments


def _tool_call_key(name: str, arguments: dict) -> str:
    """Return a stable key for exact model tool-call deduplication."""
    try:
        rendered = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = repr(arguments)
    return f"{name}:{rendered}"


def _is_protected_write_path(path: str) -> bool:
    """Whether a model-generated patch must never rewrite this path.

    Write mode pushes to the real repository with the deployment's token, so a patch
    induced by a malicious issue (or a hallucinated "fix") must not be able to touch
    CI workflows (``pull_request_target`` ⇒ code execution), container/infra files,
    lock files or secrets.
    """
    lowered = path.casefold()
    name = lowered.rsplit("/", 1)[-1]
    return (
        lowered.startswith(_PROTECTED_PATH_PREFIXES)
        or lowered.endswith(_PROTECTED_PATH_SUFFIXES)
        or name in _PROTECTED_PATH_NAMES
    )


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
    lowered_branch = normalized_branch.casefold()
    if lowered_branch in _PROTECTED_BRANCHES or lowered_branch.startswith(_PROTECTED_BRANCH_PREFIXES):
        raise ValueError("The proposal branch must not target a protected branch")
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
        if _is_protected_write_path(path):
            raise ValueError(f"Refusing to modify protected path: {path}")
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
