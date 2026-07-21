import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.github import GitHubFileSkipped
from app.models import IssueData, SourceFile
from app.tools import ToolExecutor, parse_tool_call, validate_pr_proposal

_SETTINGS = Settings(openai_api_key="test-key")


def _make_issue() -> IssueData:
    return IssueData(
        owner="acme", repo="widget", number=1, title="Bug", body="", labels=[], comments=[], default_branch="main"
    )


def _make_github(file_contents: dict[str, str] | None = None) -> MagicMock:
    github = MagicMock()
    file_contents = file_contents or {}

    async def _get_file(issue, path):
        if path not in file_contents:
            raise FileNotFoundError(path)
        return SourceFile(path=path, content=file_contents[path])

    github.get_file = AsyncMock(side_effect=_get_file)
    return github


async def test_read_file_returns_content_with_line_numbers() -> None:
    github = _make_github({"src/a.py": "line1\nline2\nline3"})
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), ["src/a.py"])
    result = await executor.execute("read_file", {"path": "src/a.py"})
    assert result == "L1: line1\nL2: line2\nL3: line3"
    assert "src/a.py" in executor.files_read
    assert "read_file" in executor.tools_used


async def test_read_file_with_line_range() -> None:
    github = _make_github({"src/a.py": "line1\nline2\nline3\nline4\nline5"})
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), ["src/a.py"])
    result = await executor.execute("read_file", {"path": "src/a.py", "start_line": 2, "end_line": 4})
    assert result == "L2: line2\nL3: line3\nL4: line4"


async def test_read_file_with_start_line_only() -> None:
    github = _make_github({"src/a.py": "line1\nline2\nline3"})
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), ["src/a.py"])
    result = await executor.execute("read_file", {"path": "src/a.py", "start_line": 2})
    assert result == "L2: line2\nL3: line3"


async def test_read_file_uses_cache_on_second_call() -> None:
    github = _make_github({"src/a.py": "content"})
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), ["src/a.py"])
    await executor.execute("read_file", {"path": "src/a.py"})
    await executor.execute("read_file", {"path": "src/a.py"})
    assert github.get_file.call_count == 1


async def test_read_file_clamps_line_range() -> None:
    github = _make_github({"src/a.py": "line1\nline2"})
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), ["src/a.py"])
    result = await executor.execute("read_file", {"path": "src/a.py", "start_line": 0, "end_line": 99})
    assert result == "L1: line1\nL2: line2"


async def test_read_file_files_read_no_duplicates() -> None:
    github = _make_github({"src/a.py": "content"})
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), ["src/a.py"])
    await executor.execute("read_file", {"path": "src/a.py"})
    await executor.execute("read_file", {"path": "src/a.py"})
    assert executor.files_read.count("src/a.py") == 1


async def test_list_directory_root() -> None:
    tree = ["src/main.py", "src/utils.py", "docs/readme.md", "README.md"]
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), tree)
    result = await executor.execute("list_directory", {"path": ""})
    entries = result.split("\n")
    assert "src/" in entries
    assert "docs/" in entries
    assert "README.md" in entries


async def test_list_directory_subdirectory() -> None:
    tree = ["src/main.py", "src/utils/helper.py", "tests/test_main.py"]
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), tree)
    result = await executor.execute("list_directory", {"path": "src"})
    entries = result.split("\n")
    assert "main.py" in entries
    assert "utils/" in entries
    assert "test_main.py" not in entries


async def test_list_directory_nonexistent() -> None:
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), ["src/a.py"])
    result = await executor.execute("list_directory", {"path": "nonexistent"})
    assert "empty" in result or "does not exist" in result


async def test_search_files_matches_keyword() -> None:
    tree = ["src/parser.py", "src/database.py", "docs/parser.md", "README.md"]
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), tree)
    result = await executor.execute("search_files", {"query": "parser"})
    assert "src/parser.py" in result
    assert "docs/parser.md" in result
    assert "database" not in result


async def test_search_files_no_match() -> None:
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), ["src/a.py"])
    result = await executor.execute("search_files", {"query": "nonexistent"})
    assert "No files" in result


async def test_tool_observations_are_bounded_and_read_files_are_not_duplicated() -> None:
    settings = Settings(openai_api_key="test-key", max_investigation_ledger_chars=1_000)
    github = _make_github({"src/a.py": "line1\nline2"})
    executor = ToolExecutor(github, settings, _make_issue(), ["src/a.py", "src/b.py"])

    await executor.execute("read_file", {"path": "src/a.py"})
    await executor.execute("search_files", {"query": "src"})

    ledger = "\n".join(executor.investigation_ledger)
    assert "source captured" in ledger
    assert "L1: line1" not in ledger
    assert "search_files" in ledger
    assert "src/a.py" in ledger
    assert len(ledger) <= settings.max_investigation_ledger_chars


def test_pr_observation_never_copies_proposed_file_contents() -> None:
    settings = Settings(openai_api_key="test-key", write_mode=True)
    executor = ToolExecutor(MagicMock(), settings, _make_issue(), ["src/a.py"])
    arguments = {
        "branch": "fix/issue-1",
        "title": "fix: parser",
        "body": "Fixes the parser.",
        "changes": [{"path": "src/a.py", "content": "SECRET-CONTENT", "message": "fix parser"}],
    }

    executor._record_observation("create_pull_request", arguments, "proposal stored")

    ledger = "\n".join(executor.investigation_ledger)
    assert "src/a.py" in ledger
    assert "SECRET-CONTENT" not in ledger


async def test_search_code_uses_repository_wide_github_search() -> None:
    github = MagicMock()
    github.search_code = AsyncMock(
        return_value=[{"path": "src/parser.py", "fragments": ["raise ParserError(message)"]}]
    )
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), ["src/parser.py"])

    result = await executor.execute("search_code", {"query": "ParserError"})

    assert "src/parser.py" in result
    assert "raise ParserError" in result
    github.search_code.assert_awaited_once()


async def test_grep_content_finds_matches_in_cached_files() -> None:
    github = _make_github({"src/a.py": "def foo():\n    return 1\n"})
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), ["src/a.py"])
    executor._file_cache["src/a.py"] = "def foo():\n    return 1\n"
    result = await executor.execute("grep_content", {"pattern": "foo"})
    assert "src/a.py:L1: def foo():" in result


async def test_grep_content_no_matches() -> None:
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), [])
    executor._file_cache["src/a.py"] = "def bar():\n    pass\n"
    result = await executor.execute("grep_content", {"pattern": "foo"})
    assert "No matches" in result


async def test_grep_content_path_filter_restricts_to_single_file() -> None:
    """Optional path argument narrows search to one already-read file."""
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), ["src/a.py", "src/b.py"])
    executor._file_cache["src/a.py"] = "def foo():\n    return 1\n"
    executor._file_cache["src/b.py"] = "def foo():\n    return 2\n"
    result = await executor.execute("grep_content", {"pattern": "foo", "path": "src/a.py"})
    assert "src/a.py:L1: def foo():" in result
    assert "src/b.py" not in result


async def test_grep_content_path_filter_rejects_unread_file() -> None:
    """Path argument pointing to an unread file returns a helpful message."""
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), ["src/a.py"])
    executor._file_cache["src/a.py"] = "content\n"
    result = await executor.execute("grep_content", {"pattern": "x", "path": "src/unread.py"})
    # _normalize_file_path rejects unknown paths before the unread-file check.
    assert "Error" in result


async def test_grep_content_invalid_regex_falls_back_to_literal() -> None:
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), [])
    executor._file_cache["src/a.py"] = "a(b)c\n"
    result = await executor.execute("grep_content", {"pattern": "a(b"})
    assert "src/a.py:L1: a(b)c" in result


async def test_execute_unknown_tool_returns_message() -> None:
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), [])
    result = await executor.execute("nonexistent_tool", {})
    assert "Unknown tool" in result


async def test_execute_handles_file_skipped_error() -> None:
    github = MagicMock()
    github.get_file = AsyncMock(side_effect=GitHubFileSkipped("too large"))
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), ["src/big.py"])
    result = await executor.execute("read_file", {"path": "src/big.py"})
    assert "File skipped" in result


async def test_execute_handles_generic_error() -> None:
    github = MagicMock()
    github.get_file = AsyncMock(side_effect=RuntimeError("network down"))
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), ["src/a.py"])
    result = await executor.execute("read_file", {"path": "src/a.py"})
    assert "Error" in result


def test_line_counts_property() -> None:
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), [])
    executor._file_cache["src/a.py"] = "line1\nline2\nline3"
    executor._file_cache["src/b.py"] = "single"
    counts = executor.line_counts
    assert counts["src/a.py"] == 3
    assert counts["src/b.py"] == 1


def test_file_cache_property_returns_cache() -> None:
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), ["src/a.py"], file_cache={"src/a.py": "content"})
    assert executor.file_cache == {"src/a.py": "content"}


def test_file_cache_copied_not_shared() -> None:
    original = {"src/a.py": "content"}
    executor = ToolExecutor(MagicMock(), _SETTINGS, _make_issue(), ["src/a.py"], file_cache=original)
    executor.file_cache["src/b.py"] = "new"
    assert "src/b.py" not in original


def test_files_read_copied_not_shared() -> None:
    original = ["src/a.py"]
    executor = ToolExecutor(
        MagicMock(), _SETTINGS, _make_issue(), ["src/a.py"], file_cache={"src/a.py": "content"}, files_read=original
    )
    executor.files_read.append("src/b.py")
    assert "src/b.py" not in original


async def test_read_file_rejects_unsafe_and_unknown_paths_before_request() -> None:
    github = _make_github({"src/a.py": "content"})
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), ["src/a.py"])
    for path in ("../secret", "src/../a.py", "/src/a.py", "src\\a.py", "unknown.py", ""):
        result = await executor.execute("read_file", {"path": path})
        assert result.startswith("Error:")
    github.get_file.assert_not_awaited()


async def test_read_file_enforces_file_and_total_character_limits() -> None:
    github = _make_github({"a.py": "abcdef", "b.py": "uvwxyz"})
    executor = ToolExecutor(
        github, _SETTINGS, _make_issue(), ["a.py", "b.py"], max_files=2, max_file_chars=4, max_total_context_chars=6
    )
    await executor.execute("read_file", {"path": "a.py"})
    await executor.execute("read_file", {"path": "b.py"})
    assert executor.file_cache == {"a.py": "abcd", "b.py": "uv"}
    assert sum(map(len, executor.file_cache.values())) == 6


async def test_read_file_can_fetch_lines_beyond_cached_prefix() -> None:
    content = "\n".join(f"line {number}" for number in range(1, 51))
    github = _make_github({"large.py": content})
    executor = ToolExecutor(
        github,
        _SETTINGS,
        _make_issue(),
        ["large.py"],
        max_file_chars=20,
    )

    result = await executor.execute("read_file", {"path": "large.py", "start_line": 40, "end_line": 42})

    assert "L40: line 40" in result
    assert "L42: line 42" in result
    assert executor.line_counts["large.py"] == 50


async def test_create_pull_request_tool_stores_validated_proposal() -> None:
    settings = Settings(openai_api_key="test-key", write_mode=True)
    executor = ToolExecutor(MagicMock(), settings, _make_issue(), ["src/a.py"])

    result = await executor._tool_create_pull_request(
        branch="fix/issue-1",
        title="Fix parser",
        body="Fixes the parser failure.",
        changes=[{"path": "src/a.py", "content": "fixed\n", "message": "fix: parser"}],
    )

    assert "PR PROPOSAL" in result
    assert executor.pr_proposal == {
        "branch": "fix/issue-1",
        "title": "Fix parser",
        "body": "Fixes the parser failure.",
        "changes": [{"path": "src/a.py", "content": "fixed\n", "message": "fix: parser"}],
    }


async def test_create_pull_request_tool_rejects_unsafe_paths() -> None:
    settings = Settings(openai_api_key="test-key", write_mode=True)
    executor = ToolExecutor(MagicMock(), settings, _make_issue(), [])

    result = await executor._tool_create_pull_request(
        branch="fix/issue-1",
        title="Fix parser",
        body="Fixes the parser failure.",
        changes=[{"path": "../secret", "content": "x", "message": "fix: parser"}],
    )

    assert result.startswith("Error:")
    assert executor.pr_proposal is None


def test_pr_proposal_rejects_default_branch_and_total_size_limit() -> None:
    settings = Settings(
        openai_api_key="test-key",
        write_mode=True,
        max_pr_total_bytes=4096,
        github_max_file_bytes=10_000,
    )

    with pytest.raises(ValueError, match="default branch"):
        validate_pr_proposal(
            settings,
            branch="main",
            title="Fix",
            body="Body",
            changes=[{"path": "src/a.py", "content": "fixed", "message": "fix: parser"}],
            default_branch="main",
        )
    with pytest.raises(ValueError, match="total PR size"):
        validate_pr_proposal(
            settings,
            branch="fix/issue-1",
            title="Fix",
            body="Body",
            changes=[{"path": "src/a.py", "content": "x" * 4097, "message": "fix: parser"}],
            default_branch="main",
        )


def test_restored_cache_is_filtered_and_bounded_without_files_read() -> None:
    executor = ToolExecutor(
        MagicMock(),
        _SETTINGS,
        _make_issue(),
        ["a.py", "b.py"],
        file_cache={"a.py": "abcdef", "missing.py": "ignored", "b.py": "uvwxyz"},
        max_files=2,
        max_file_chars=4,
        max_total_context_chars=6,
    )
    assert executor.file_cache == {"a.py": "abcd", "b.py": "uv"}
    assert executor.files_read == ["a.py", "b.py"]


def test_parse_tool_call_valid() -> None:
    tc = SimpleNamespace(function=SimpleNamespace(name="read_file", arguments='{"path": "src/a.py"}'))
    name, args = parse_tool_call(tc)
    assert name == "read_file"
    assert args == {"path": "src/a.py"}


def test_parse_tool_call_invalid_json() -> None:
    tc = SimpleNamespace(function=SimpleNamespace(name="read_file", arguments="not json"))
    name, args = parse_tool_call(tc)
    assert name == "read_file"
    assert args == {}


def test_parse_tool_call_none_arguments() -> None:
    tc = SimpleNamespace(function=SimpleNamespace(name="list_directory", arguments=None))
    name, args = parse_tool_call(tc)
    assert name == "list_directory"
    assert args == {}


async def test_dispatch_resolves_via_tool_name_convention() -> None:
    """All registered tools are reachable through the _tool_<name> convention.

    This guards against regressions where a tool method is renamed without
    updating the dispatch table — the getattr lookup would return None and
    the tool would silently become 'Unknown tool'.
    """
    github = _make_github({"src/a.py": "content"})
    tree = ["src/a.py"]
    executor = ToolExecutor(github, _SETTINGS, _make_issue(), tree)

    for tool_name in (
        "read_file",
        "list_directory",
        "search_files",
        "grep_content",
        "get_file_history",
        "list_branches",
        "get_file_at_commit",
    ):
        handler = getattr(executor, f"_tool_{tool_name}", None)
        assert handler is not None, f"Tool '{tool_name}' has no _tool_ method"


async def test_parallel_preload_respects_total_context_budget() -> None:
    """Concurrent _tool_read_file calls must not collectively exceed max_total_context_chars.

    Before the re-check fix, all parallel tasks read the same stale _cached_chars
    and could overshoot the budget by up to max_files * max_file_chars.
    """
    file_contents = {f"src/file_{i}.py": "x" * 2000 for i in range(8)}
    github = _make_github(file_contents)
    tree = list(file_contents.keys())
    executor = ToolExecutor(
        github,
        _SETTINGS,
        _make_issue(),
        tree,
        max_files=8,
        max_file_chars=2000,
        max_total_context_chars=5000,
    )

    await asyncio.gather(*[executor.execute("read_file", {"path": path}) for path in tree])

    total_cached = sum(len(content) for content in executor.file_cache.values())
    assert total_cached <= 5000, f"Budget exceeded: {total_cached} > 5000"
    assert len(executor.files_read) <= 8


async def test_parallel_preload_respects_file_count_limit() -> None:
    """Concurrent reads must not overshoot the max_files limit."""
    file_contents = {f"src/f{i}.py": "content" for i in range(10)}
    github = _make_github(file_contents)
    tree = list(file_contents.keys())
    executor = ToolExecutor(
        github,
        _SETTINGS,
        _make_issue(),
        tree,
        max_files=3,
        max_file_chars=100,
        max_total_context_chars=10000,
    )

    results = await asyncio.gather(*[executor.execute("read_file", {"path": path}) for path in tree])

    cached_count = len(executor.files_read)
    assert cached_count <= 3, f"File count exceeded: {cached_count} > 3"
    limit_reached_count = sum(1 for r in results if "File limit reached" in r)
    assert limit_reached_count == 7
