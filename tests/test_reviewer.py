import json

import pytest

from app.config import Settings
from app.models import AnalysisReport, CodeReference
from app.reviewer import ReviewerAgent, ReviewResponseError, _build_review_context


def _report() -> AnalysisReport:
    return AnalysisReport(
        summary="Parser bug",
        root_cause="The parser dereferences an empty token at src/parser.py:L2.",
        confidence="medium",
        evidence=[CodeReference(path="src/parser.py", lines="L2", reason="Direct dereference")],
        proposed_changes=["Guard empty input"],
        patch=None,
        tests=["Exercise an empty token"],
        risks=[],
    )


async def test_reviewer_approves_and_records_independent_audit(fake_client, fake_response, make_issue) -> None:
    reviewed_report = _report().model_dump()
    payload = json.dumps(
        {
            "verdict": "approved",
            "summary": "Evidence directly supports the causal chain.",
            "findings": ["The proposed regression test exercises the failure path."],
            "report": reviewed_report,
        }
    )
    client = fake_client([fake_response(content=payload)])
    reviewer = ReviewerAgent(
        Settings(openai_api_key="test-key", review_model="review-test-model", language="en"),
        client,
    )

    outcome = await reviewer.review(
        issue=make_issue(),
        report=_report(),
        file_cache={"src/parser.py": "def parse(tokens):\n    return tokens[0]\n"},
        files_read=["src/parser.py"],
        line_counts={"src/parser.py": 2},
    )

    assert outcome.verdict == "approved"
    assert outcome.report.review_audit.status == "approved"
    assert outcome.report.review_audit.reviewer_model == "review-test-model"
    assert outcome.report.evidence_audit.valid_references == 1
    call = client.chat.completions.calls[0]
    # 第一次沿用全局 thinking 配置（默认 enabled）保留推理深度
    assert call["extra_body"] == {"thinking": {"type": "enabled"}}
    assert call["reasoning_effort"] == "high"
    # thinking enabled 时 provider 不设置 temperature（走 reasoning_effort 分支）
    assert "temperature" not in call
    assert "Write every human-readable field in English" in call["messages"][0]["content"]
    assert "SOURCE EXCERPTS" in call["messages"][1]["content"]


async def test_reviewer_filters_invented_evidence(fake_client, fake_response, make_issue) -> None:
    revised = _report().model_dump()
    revised["evidence"].append({"path": "src/invented.py", "lines": "L1", "reason": "Not supplied to the reviewer"})
    payload = json.dumps(
        {
            "verdict": "revised",
            "summary": "Removed an unsupported claim.",
            "findings": ["One claim lacked supplied source evidence."],
            "report": revised,
        }
    )
    reviewer = ReviewerAgent(Settings(openai_api_key="test-key"), fake_client([fake_response(content=payload)]))

    outcome = await reviewer.review(
        issue=make_issue(),
        report=_report(),
        file_cache={"src/parser.py": "def parse(tokens):\n    return tokens[0]\n"},
        files_read=["src/parser.py"],
        line_counts={"src/parser.py": 2},
    )

    assert outcome.verdict == "revised"
    assert [evidence.path for evidence in outcome.report.evidence] == ["src/parser.py"]


async def test_reviewer_normalizes_changed_approved_report_to_revised(fake_client, fake_response, make_issue) -> None:
    changed = _report().model_dump()
    changed["summary"] = "Reviewer changed this summary"
    payload = json.dumps(
        {
            "verdict": "approved",
            "summary": "Looks good.",
            "findings": [],
            "report": changed,
        }
    )
    reviewer = ReviewerAgent(
        Settings(openai_api_key="test-key", language="en"),
        fake_client([fake_response(content=payload)]),
    )

    outcome = await reviewer.review(
        issue=make_issue(),
        report=_report(),
        file_cache={"src/parser.py": "def parse(tokens):\n    return tokens[0]\n"},
        files_read=["src/parser.py"],
        line_counts={"src/parser.py": 2},
    )

    assert outcome.verdict == "revised"
    assert "normalized to revised" in outcome.findings[-1]


async def test_reviewer_rejects_invalid_response(fake_client, fake_response, make_issue) -> None:
    # max_report_retries=2 时需要两个无效响应让两次尝试都失败
    reviewer = ReviewerAgent(
        Settings(openai_api_key="test-key", max_report_retries=2),
        fake_client([fake_response(content="{}"), fake_response(content="{}")]),
    )

    with pytest.raises(ReviewResponseError):
        await reviewer.review(
            issue=make_issue(),
            report=_report(),
            file_cache={},
            files_read=[],
            line_counts={},
        )


def test_review_context_is_bounded(make_issue) -> None:
    context = _build_review_context(
        make_issue(),
        _report(),
        {"src/parser.py": "x" * 20_000},
        ["src/parser.py"],
        4_000,
    )

    assert len(context) <= 4_000


def test_legacy_report_without_review_metadata_remains_compatible() -> None:
    payload = _report().model_dump(exclude={"review_audit"})

    restored = AnalysisReport.model_validate(payload)

    assert restored.review_audit.status == "not_run"
