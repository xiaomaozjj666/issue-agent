from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.github import GitHubFileSkipped
from app.models import IssueData, SourceFile
from app.tools import ToolExecutor, parse_tool_call

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


def test_create_pull_request_tool_stores_validated_proposal() -> None:
    settings = Settings(openai_api_key="test-key", write_mode=True)
    executor = ToolExecutor(MagicMock(), settings, _make_issue(), ["src/a.py"])

    result = executor._create_pull_request_proposal(
        "fix/issue-1",
        "Fix parser",
        "Fixes the parser failure.",
        [{"path": "src/a.py", "content": "fixed\n", "message": "fix: parser"}],
    )

    assert "PR PROPOSAL" in result
    assert executor.pr_proposal == {
        "branch": "fix/issue-1",
        "title": "Fix parser",
        "body": "Fixes the parser failure.",
        "changes": [{"path": "src/a.py", "content": "fixed\n", "message": "fix: parser"}],
    }


def test_create_pull_request_tool_rejects_unsafe_paths() -> None:
    settings = Settings(openai_api_key="test-key", write_mode=True)
    executor = ToolExecutor(MagicMock(), settings, _make_issue(), [])

    result = executor._create_pull_request_proposal(
        "fix/issue-1",
        "Fix parser",
        "Fixes the parser failure.",
        [{"path": "../secret", "content": "x", "message": "fix: parser"}],
    )

    assert result.startswith("Error:")
    assert executor.pr_proposal is None


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
