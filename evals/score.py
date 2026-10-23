"""Deterministic scoring helpers for eval cases (no network, no LLM)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.models import AnalysisReport

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def load_case(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Eval case must be a JSON object")
    if "issue_url" not in data or "expected" not in data:
        raise ValueError("Eval case requires 'issue_url' and 'expected'")
    return data


def score_report(case: dict[str, Any], report: AnalysisReport) -> dict[str, Any]:
    """Return a structured pass/fail breakdown for one investigation report."""
    expected = case.get("expected") or {}
    checks: dict[str, Any] = {}

    keywords = [str(k).lower() for k in expected.get("root_cause_keywords") or []]
    haystack = f"{report.summary}\n{report.root_cause}".lower()
    keyword_hits = [k for k in keywords if k in haystack]
    checks["root_cause_keywords"] = {
        "required": keywords,
        "hits": keyword_hits,
        "passed": (not keywords) or bool(keyword_hits),
    }

    path_substrings = [str(s).lower() for s in expected.get("evidence_path_substrings") or []]
    evidence_paths = [ev.path.lower() for ev in report.evidence]
    path_hits = [sub for sub in path_substrings if any(sub in path for path in evidence_paths)]
    checks["evidence_paths"] = {
        "required": path_substrings,
        "hits": path_hits,
        "passed": (not path_substrings) or bool(path_hits),
    }

    min_confidence = expected.get("min_confidence") or "low"
    min_rank = _CONFIDENCE_RANK.get(str(min_confidence), 0)
    actual_rank = _CONFIDENCE_RANK.get(report.confidence, 0)
    checks["min_confidence"] = {
        "required": min_confidence,
        "actual": report.confidence,
        "passed": actual_rank >= min_rank,
    }

    if expected.get("require_patch"):
        checks["require_patch"] = {
            "passed": bool(report.patch and str(report.patch).strip()),
        }

    passed = all(bool(item.get("passed")) for item in checks.values())
    return {
        "case_id": case.get("id") or case.get("issue_url"),
        "issue_url": case.get("issue_url"),
        "passed": passed,
        "checks": checks,
        "metrics_snapshot": {
            "evidence_count": len(report.evidence),
            "confidence": report.confidence,
            "has_patch": bool(report.patch),
            "review_status": report.review_audit.status,
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Score a saved report against a golden case, entirely offline (no network, no LLM)."""
    parser = argparse.ArgumentParser(
        description="Score a saved AnalysisReport JSON against an eval case",
    )
    parser.add_argument("--case", type=Path, help="Path to the eval case JSON")
    parser.add_argument("--report", type=Path, help="Path to a saved AnalysisReport JSON")
    args = parser.parse_args(argv)

    if args.case is None and args.report is None:
        print("Usage: python -m evals.score --case <case.json> --report <report.json>")
        print("\nAvailable cases:")
        for path in sorted((Path(__file__).resolve().parent / "cases").glob("*.json")):
            case = load_case(path)
            print(f"  {case.get('id') or path.stem}  {case['issue_url']}")
        return 0
    if args.case is None or args.report is None:
        parser.error("--case and --report must be provided together")

    case = load_case(args.case)
    report = AnalysisReport.model_validate_json(args.report.read_text(encoding="utf-8"))
    result = score_report(case, report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
