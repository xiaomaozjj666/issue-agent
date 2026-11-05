"""Regression coverage for source completeness, hard deadlines, and ReDoS defenses."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent import IssueAgent
from app.config import Settings
from app.evidence import EvidenceValidator
from app.models import AnalysisReport, CodeReference, IssueData, SourceFile
from app.report_generator import ReportGenerator
from app.reviewer import _build_review_context
from app.sessions import Session
from app.source_context import (
    _add_lines,
    _complement,
    bound_text_head_tail,
    merge_line_ranges,
    render_numbered_source,
    sample_source,
)
from app.tools import ToolExecutor, _regex_has_quantifier, _regex_refusal_reason


def _issue() -> IssueData:
    return IssueData(
        owner="acme",
        repo="widget",
        number=1,
        title="Bug",
        body="",
        labels=[],
        comments=[],
        default_branch="main",
    )


def _executor(content: str, *, max_file_chars: int = 1_000) -> ToolExecutor:
    github = MagicMock()
    github.get_file = AsyncMock(return_value=SourceFile(path="src/large.py", content=content))
    settings = Settings(openai_api_key="test-key", max_total_context_chars=20_000)
    return ToolExecutor(
        github,
        settings,
        _issue(),
        ["src/large.py"],
        max_file_chars=max_file_chars,
        max_total_context_chars=5_000,
    )


def _report(lines: str, symbol: str) -> AnalysisReport:
    return AnalysisReport(
        summary=f"{symbol} failure",
        root_cause=f"{symbol} is raised on the affected input",
        confidence="low",
        evidence=[
            CodeReference(
                path="src/large.py",
                lines=lines,
                reason=f"{symbol} handles token input",
                strength="strong",
            )
        ],
        proposed_changes=["Add a guard"],
        tests=["Cover the failing input"],
        risks=[],
    )


def test_structured_sample_retains_head_tail_and_focus_within_budget() -> None:
    content = "\n".join(f"line {number}" for number in range(1, 501))

    sample = sample_source(content, 700, [(248, 252)])
    rendered = render_numbered_source(sample.text)

    assert len(sample.text) <= 700
    assert "L1: line 1" in rendered
    assert "L250: line 250" in rendered
    assert "L500: line 500" in rendered
    assert sample.omitted_ranges


async def test_long_file_tail_reaches_final_report_with_coverage_metadata(fake_client, fake_response) -> None:
    lines = [f"ordinary line {number}" for number in range(1, 500)]
    lines.append('raise TailRuntimeError("tail bug")')
    executor = _executor("\n".join(lines))
    await executor.execute("read_file", {"path": "src/large.py"})
    settings = Settings(openai_api_key="test-key", max_total_context_chars=20_000)
    payload = _report("L500", "TailRuntimeError").model_dump_json()
    client = fake_client([fake_response(content=payload)])

    report = await ReportGenerator(settings, client).generate([], executor)

    prompt = client.chat.completions.calls[0]["messages"][-1]["content"]
    assert "L500: raise TailRuntimeError" in prompt
    assert "Source truncation metadata" in prompt
    assert report.evidence[0].lines == "L500"
    assert report.evidence[0].strength == "strong"
    assert report.source_coverage[0].path == "src/large.py"
    assert report.source_coverage[0].omitted_ranges
    assert report.source_coverage[0].captured_chars < report.source_coverage[0].original_chars


async def test_grep_middle_hit_is_promoted_to_report_and_independent_review() -> None:
    lines = [f"ordinary line {number}" for number in range(1, 501)]
    lines[249] = "def middle_failure(token): raise MiddleInputError(token)"
    executor = _executor("\n".join(lines))
    await executor.execute("read_file", {"path": "src/large.py"})

    result = await executor.execute("grep_content", {"pattern": "middle_failure"})
    report = _report("L250", "middle_failure")
    validated = EvidenceValidator().validate(
        report,
        files_read=executor.files_read,
        line_counts=executor.line_counts,
        file_cache=executor.file_cache,
        source_coverage=executor.source_coverage,
    )
    report_messages = ReportGenerator(executor._settings, MagicMock()).build_report_messages([], executor)
    review_context = _build_review_context(
        _issue(),
        validated,
        executor.file_cache,
        executor.files_read,
        12_000,
        executor.source_coverage,
    )

    assert "src/large.py:L250: def middle_failure" in result
    assert "L250: def middle_failure" in render_numbered_source(executor.file_cache["src/large.py"])
    assert validated.evidence[0].strength == "strong"
    for context in (report_messages[-1]["content"], review_context):
        assert "L250: def middle_failure" in context
        assert "L500: ordinary line 500" in context
    assert "SOURCE TRUNCATION METADATA" in review_context


class _SlowGitHub:
    cache_hits = 0
    cache_misses = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get_issue(self, *_args):
        await asyncio.sleep(30)
        raise AssertionError("the global deadline did not cancel get_issue")


async def test_investigation_deadline_cancels_inflight_fetch_and_marks_session(fake_client) -> None:
    from typing import cast

    from app.github import GitHubClient

    agent = IssueAgent(
        Settings(openai_api_key="test-key", investigation_timeout=0.03, independent_review=False),
        client=fake_client([]),
        github_client=cast(GitHubClient, _SlowGitHub()),
    )
    session = Session(session_id="hard-timeout", issue_url="https://github.com/acme/widget/issues/1")
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="total timeout"):
        async for _event in agent.investigate_stream(session.issue_url, session=session):
            pass

    assert time.monotonic() - started < 0.5
    assert session.status == "failed"
    assert session.phase == "timeout"
    assert session.metrics["timed_out"] is True
    assert session.metrics["timeout_seconds"] == 0.03
    assert "total timeout" in (session.error_message or "")


@pytest.mark.parametrize("pattern", ["(a|aa)+$", "(a|a|ab)*$", "(ab|aab)+$", "a" * 300])
async def test_grep_rejects_adversarial_regex_complexity(pattern: str) -> None:
    executor = _executor("a" * 100)
    executor._file_cache["src/large.py"] = "a" * 100

    result = await executor._tool_grep_content(pattern)

    assert result.startswith("Refusing to run this pattern")
    assert "catastrophic backtracking" in result


async def test_grep_execution_timeout_prevents_thread_exhaustion(monkeypatch) -> None:
    executor = _executor("safe text")
    executor._file_cache["src/large.py"] = "safe text"
    calls = 0

    def slow_scan(_regex, _cache):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return [], {}

    monkeypatch.setattr("app.tools._GREP_TIMEOUT_SECONDS", 0.005)
    monkeypatch.setattr(executor, "_grep_lines", slow_scan)

    first = await executor._tool_grep_content("safe")
    second = await executor._tool_grep_content("safe")
    await asyncio.sleep(0.06)  # let the bounded worker finish before the next event loop

    assert "timed out" in first
    assert "previous regex timed out" in second
    assert calls == 1


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("a+", True),
        ("a*", True),
        ("a?", True),
        ("a{2,3}", True),
        ("a{b}", False),
        ("(?:a)", False),
        ("[+]", False),
        ("\\+", False),
    ],
)
def test_regex_has_quantifier_detects_engine_metacharacters(pattern: str, expected: bool) -> None:
    assert _regex_has_quantifier(pattern) is expected


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("(a|b)*", "repeated alternation with ambiguous branches"),
        ("(ab{2})+", "nested quantifiers"),
        ("(a?)+", "nested quantifiers"),
        ("(a+)+", "nested quantifiers"),
        ("(a\\|b)*", None),
        ("([|])*", None),
        ("[(a)]*", None),
        ("plain", None),
        ("a" * 257, "patterns longer than 256 characters"),
    ],
)
def test_regex_refusal_reason_blocks_backtracking_shapes(pattern: str, expected: str | None) -> None:
    assert _regex_refusal_reason(pattern) == expected


def test_merge_line_ranges_swaps_reversed_and_merges_adjacent() -> None:
    assert merge_line_ranges([(5, 2)], 10) == [(2, 5)]
    assert merge_line_ranges([(7, 8), (9, 10)], 20) == [(7, 10)]


def test_add_lines_skips_selected_lines_and_crops_one_long_line() -> None:
    selected: dict[int, str] = {}
    _add_lines(["a", "b", "c"], selected, 1, 3, 1_000)
    _add_lines(["a", "b", "c"], selected, 1, 3, 1_000, reverse=True)
    assert selected == {0: "a", 1: "b", 2: "c"}

    cropped: dict[int, str] = {}
    _add_lines(["x" * 400], cropped, 1, 1, 100)
    assert cropped == {0: "x" * 99}


def test_complement_reports_tail_gap() -> None:
    assert _complement([(1, 2)], 5) == [(3, 5)]


def test_bound_text_head_tail_short_budget_keeps_prefix() -> None:
    assert bound_text_head_tail("x" * 200, 50) == "x" * 50
