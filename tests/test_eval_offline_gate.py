import json
from pathlib import Path

from evals.offline_gate import main, run_offline_gate


def test_committed_offline_eval_fixture_passes_quality_gate(capsys) -> None:
    assert main(["--min-pass-rate", "1.0"]) == 0
    assert "100.0%" in capsys.readouterr().out


def test_offline_eval_quality_gate_fails_on_regression(tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    reports = tmp_path / "reports"
    cases.mkdir()
    reports.mkdir()
    case = {
        "id": "known-root-cause",
        "issue_url": "https://github.com/acme/widget/issues/1",
        "expected": {"root_cause_keywords": ["expected-cause"], "min_confidence": "medium"},
    }
    report = {
        "summary": "wrong answer",
        "root_cause": "unrelated",
        "confidence": "low",
        "evidence": [],
        "proposed_changes": [],
        "tests": [],
        "risks": [],
    }
    (cases / "case.json").write_text(json.dumps(case), encoding="utf-8")
    (reports / "case.json").write_text(json.dumps(report), encoding="utf-8")

    summary = run_offline_gate(cases, reports)

    assert summary["total"] == 1
    assert summary["pass_rate"] == 0.0
    assert summary["gate_passed"] is False
    assert main(["--cases-dir", str(cases), "--reports-dir", str(reports)]) == 1
