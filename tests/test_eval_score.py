import json
from pathlib import Path

from app.models import AnalysisReport, CodeReference
from evals.score import load_case, score_report

_CASES_DIR = Path(__file__).resolve().parent.parent / "evals" / "cases"


def test_demo_case_is_valid() -> None:
    case = load_case(_CASES_DIR / "demo-synthetic-001.json")
    assert case["issue_url"].startswith("https://github.com/")
    assert "expected" in case


def test_score_report_passes_all_checks() -> None:
    case = {
        "id": "t1",
        "issue_url": "https://github.com/a/b/issues/1",
        "expected": {
            "root_cause_keywords": ["null pointer"],
            "evidence_path_substrings": ["src/auth"],
            "min_confidence": "medium",
            "require_patch": True,
        },
    }
    report = AnalysisReport(
        summary="Null pointer in auth",
        root_cause="A null pointer is raised when session is missing",
        confidence="high",
        evidence=[CodeReference(path="src/auth/login.py", lines="L10", reason="check")],
        proposed_changes=["add nil check"],
        tests=["t"],
        risks=[],
        patch="--- a\n+++ b\n",
    )
    result = score_report(case, report)
    assert result["passed"] is True
    assert result["checks"]["root_cause_keywords"]["passed"] is True
    assert result["checks"]["evidence_paths"]["passed"] is True
    assert result["checks"]["min_confidence"]["passed"] is True
    assert result["checks"]["require_patch"]["passed"] is True


def test_score_report_fails_on_weak_confidence_and_missing_patch() -> None:
    case = {
        "id": "t2",
        "issue_url": "https://github.com/a/b/issues/2",
        "expected": {
            "root_cause_keywords": ["quantum"],
            "min_confidence": "high",
            "require_patch": True,
        },
    }
    report = AnalysisReport(
        summary="something",
        root_cause="unrelated text",
        confidence="low",
        evidence=[],
        proposed_changes=[],
        tests=[],
        risks=[],
        patch=None,
    )
    result = score_report(case, report)
    assert result["passed"] is False
    assert result["checks"]["root_cause_keywords"]["passed"] is False
    assert result["checks"]["min_confidence"]["passed"] is False
    assert result["checks"]["require_patch"]["passed"] is False


def test_score_report_skips_empty_optional_checks() -> None:
    case = {"id": "t3", "issue_url": "https://github.com/a/b/issues/3", "expected": {}}
    report = AnalysisReport(
        summary="s",
        root_cause="r",
        confidence="low",
        evidence=[],
        proposed_changes=[],
        tests=[],
        risks=[],
    )
    assert score_report(case, report)["passed"] is True


def test_load_case_rejects_invalid_shape(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"issue_url": "https://x"}), encoding="utf-8")
    try:
        load_case(bad)
    except ValueError as exc:
        assert "expected" in str(exc)
    else:
        raise AssertionError("expected ValueError")
