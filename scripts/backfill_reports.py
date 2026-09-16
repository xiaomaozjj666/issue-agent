#!/usr/bin/env python3
"""One-off backfill for historical session reports.

Adds the enrichment fields introduced by the "senior investigator" report
schema (impact / hypotheses / evidence strength / confidence & fix rationale)
to any stored report that lacks them. Idempotent and non-destructive:
the DB is copied to ``sessions.db.bak`` before anything is written, and each
enriched report is validated against the pydantic model before persisting.

Usage (from project root):
    python scripts/backfill_reports.py

The database path is resolved through ``app.config.get_settings()`` so it honours
``SESSION_DB_PATH`` (and ``.env``) instead of assuming ``data/sessions.db``.

The script deliberately avoids importing the ``app`` *package* (which would pull
in the FastAPI stack via ``app/__init__.py``) by loading ``app/config.py`` as a
standalone module; the remaining modules are imported as top-level names with
``app/`` on ``sys.path``.
"""

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"


def _load_settings():
    """Load ``app.config`` without executing the ``app`` package ``__init__``.

    ``app/__init__.py`` imports the whole FastAPI stack; registering the module
    under the real ``app.config`` name before execution keeps the import cheap
    while still resolving the same module object.
    """
    # Settings requires a non-empty API key; this script never calls the LLM.
    os.environ.setdefault("OPENAI_API_KEY", "backfill-placeholder")
    spec = importlib.util.spec_from_file_location("app.config", APP_DIR / "config.py")
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery guard
        raise ImportError(f"cannot load app.config from {APP_DIR / 'config.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.config"] = module
    spec.loader.exec_module(module)
    return module.get_settings()


sys.path.insert(0, str(APP_DIR))

import report_backfill  # noqa: E402  (standalone import, no package init)
from models import AnalysisReport  # noqa: E402  (validation only)


def _backup_database(db_path: Path, backup: Path) -> None:
    """Copy the database, checkpointing WAL first.

    ``shutil.copy`` only sees the main database file; in WAL mode the freshest
    writes may still live in ``-wal``. ``wal_checkpoint(TRUNCATE)`` flushes them
    into the main file so a single-file copy is complete.
    """
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.commit()
    finally:
        con.close()
    shutil.copy(db_path, backup)


def main() -> int:
    settings = _load_settings()
    db_path = Path(settings.session_db_path)

    if not db_path.is_absolute():
        db_path = ROOT / db_path

    if not db_path.exists():
        print(
            f"ERROR: no database found at {db_path}\n"
            f"       resolved from SESSION_DB_PATH={settings.session_db_path!r}\n"
            "       Set SESSION_DB_PATH or run the app once to create it.",
            file=sys.stderr,
        )
        return 1

    backup = db_path.with_suffix(".db.bak")
    _backup_database(db_path, backup)
    print(f"Backed up database to {backup}")

    con = sqlite3.connect(db_path)
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
