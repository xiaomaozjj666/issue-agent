"""Deterministic offline eval quality gate for CI (no network or LLM calls)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.models import AnalysisReport
from evals.score import load_case, score_report

_CASES_DIR = Path(__file__).resolve().parent / "cases"
_REPORTS_DIR = Path(__file__).resolve().parent / "fixtures"


def run_offline_gate(cases_dir: Path, reports_dir: Path, min_pass_rate: float = 1.0) -> dict[str, Any]:
    """Score every case with a same-stem saved report and enforce the threshold."""
    results: list[dict[str, Any]] = []
    for case_path in sorted(cases_dir.glob("*.json")):
        report_path = reports_dir / f"{case_path.stem}.json"
        if not report_path.is_file():
            continue
        case = load_case(case_path)
        report = AnalysisReport.model_validate_json(report_path.read_text(encoding="utf-8"))
        results.append(score_report(case, report))

    passed = sum(bool(result["passed"]) for result in results)
    total = len(results)
    pass_rate = passed / total if total else 0.0
    return {
        "results": results,
        "passed": passed,
        "total": total,
        "pass_rate": pass_rate,
        "min_pass_rate": min_pass_rate,
        "gate_passed": bool(results) and pass_rate >= min_pass_rate,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic offline eval fixtures as a quality gate")
    parser.add_argument("--cases-dir", type=Path, default=_CASES_DIR)
    parser.add_argument("--reports-dir", type=Path, default=_REPORTS_DIR)
    parser.add_argument("--min-pass-rate", type=float, default=1.0)
    args = parser.parse_args(argv)
    if not 0.0 <= args.min_pass_rate <= 1.0:
        parser.error("--min-pass-rate must be between 0 and 1")

    summary = run_offline_gate(args.cases_dir, args.reports_dir, args.min_pass_rate)
    for result in summary["results"]:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{status} {result['case_id']}: {json.dumps(result['checks'], ensure_ascii=False)}")
    print(
        f"Offline eval: {summary['passed']}/{summary['total']} passed "
        f"({summary['pass_rate']:.1%}; required {summary['min_pass_rate']:.1%})"
    )
    if not summary["results"]:
        print("No offline eval fixtures found.", flush=True)
        return 2
    return 0 if summary["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
