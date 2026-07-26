#!/usr/bin/env python3
"""One-off backfill for historical session reports.

Adds the enrichment fields introduced by the "senior investigator" report
schema (impact / hypotheses / evidence strength / confidence & fix rationale)
to any stored report that lacks them. Idempotent and non-destructive:
the DB is copied to ``sessions.db.bak`` before anything is written, and each
enriched report is validated against the pydantic model before persisting.

Usage (from project root):
    python scripts/backfill_reports.py

It deliberately avoids importing the ``app`` package (which would pull in the
FastAPI stack) by adding ``app/`` to ``sys.path`` and importing the modules as
top-level names.
"""

import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
DB_PATH = ROOT / "data" / "sessions.db"

sys.path.insert(0, str(APP_DIR))

import report_backfill  # noqa: E402  (standalone import, no package init)
from models import AnalysisReport  # noqa: E402  (validation only)


def main() -> int:
    if not DB_PATH.exists():
        print(f"No database found at {DB_PATH}; nothing to do.")
        return 0

    backup = DB_PATH.with_suffix(".db.bak")
    shutil.copy(DB_PATH, backup)
    print(f"Backed up database to {backup}")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT session_id, report_json FROM sessions WHERE report_json IS NOT NULL"
    ).fetchall()

    total = len(rows)
    changed = 0
    errors = 0
    for row in rows:
        sid, rj = row["session_id"], row["report_json"]
        try:
            rep = json.loads(rj)
        except (ValueError, TypeError):
            continue
        enriched = report_backfill.enrich_report(rep)
        try:
            AnalysisReport.model_validate(enriched)
        except Exception as exc:  # validation guard — never write an invalid report
            print(f"  [skip] {sid}: validation failed: {exc}")
            errors += 1
            continue
        new_rj = json.dumps(enriched, ensure_ascii=False)
        if new_rj != rj:
            con.execute(
                "UPDATE sessions SET report_json = ? WHERE session_id = ?",
                (new_rj, sid),
            )
            changed += 1
    con.commit()
    con.close()

    print(f"Reports scanned: {total} | enriched: {changed} | errors: {errors}")
    print("Done. Re-open a historical session to see the new charts and sections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
