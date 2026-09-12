"""Score investigation reports against golden cases.

Usage (live scoring, needs OPENAI_API_KEY and network):

    python -m evals.run_eval --case evals/cases/my-case.json
    python -m evals.run_eval --dir evals/cases --limit 5

Offline helpers (no network) live in ``evals.score`` and are covered by unit tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from evals.score import load_case, score_report

_CASES_DIR = Path(__file__).resolve().parent / "cases"


def _iter_case_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


async def _run_case(case_path: Path) -> dict:
    from app.agent import IssueAgent
    from app.config import Settings

    case = load_case(case_path)
    settings = Settings()
    agent = IssueAgent(settings)
    try:
        report = await agent.investigate(case["issue_url"])
    finally:
        await agent.aclose()
    return score_report(case, report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run issue-agent eval cases")
    parser.add_argument("--case", action="append", default=[], help="Path to a single case JSON (repeatable)")
    parser.add_argument("--dir", type=Path, default=_CASES_DIR, help="Directory of case JSON files")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N cases (0 = all)")
    args = parser.parse_args(argv)

    files: list[Path] = [Path(p) for p in args.case]
    if not files:
        files = _iter_case_files(args.dir)
    if args.limit > 0:
        files = files[: args.limit]
    if not files:
        print("No eval cases found.", file=sys.stderr)
        return 2

    results = []
    for path in files:
        print(f"Running {path.name} ...", flush=True)
        try:
            result = asyncio.run(_run_case(path))
        except Exception as exc:  # noqa: BLE001 — report per-case failure and continue
            result = {
                "case_id": path.stem,
                "passed": False,
                "error": str(exc)[:500],
                "checks": {},
            }
        results.append(result)
        status = "PASS" if result.get("passed") else "FAIL"
        print(f"  {status}: {json.dumps(result.get('checks', {}), ensure_ascii=False)}")

    passed = sum(1 for r in results if r.get("passed"))
    print(f"\n{passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
