"""Backfill enriched report fields for historical sessions.

Older reports (created before the "senior investigator" schema) only contain
the original fields. This module derives the *optional* enrichment fields from
data that already exists in the report, so historical sessions also benefit
from the new charts and sections — without fabricating investigation reasoning.

Everything here is pure stdlib on purpose: it must be importable both as a
standalone module (for the one-off ``scripts/backfill_reports.py``) and from
``app.db`` at runtime, without pulling in the FastAPI stack.

Design rules (honesty):
- We only FILL MISSING optional fields; we never overwrite data the LLM produced.
- ``blast_radius`` is derived from the patch / evidence / files_examined paths.
- ``confidence_rationale`` / ``fix_rationale`` are synthesized from real signals
  (evidence count, file count, presence of patch/tests).
- Evidence ``strength``/``kind`` are inferred from the path; evidence on a file
  the fix touches is marked ``strong``.
- ``hypotheses`` gets one *accepted* entry equal to the root cause (truthful);
  we do NOT invent rejected alternatives we cannot substantiate.
- ``reproduction`` is left empty — it cannot be derived honestly from a finished
  report, and a fabricated repro would undermine the very rigor we are adding.
"""

import json
import re

_CJK = re.compile(r"[一-鿿]")
_CONFIG_EXT = (".yml", ".yaml", ".toml", ".ini", ".cfg", ".env", ".conf", ".properties")


def detect_lang(rep: dict) -> str:
    text = " ".join(
        [rep.get("summary", ""), rep.get("root_cause", ""), rep.get("confidence_rationale", "")]
    )
    return "zh" if _CJK.search(text) else "en"


def infer_kind(path: str) -> str:
    p = (path or "").lower()
    name = p.split("/")[-1].split("\\")[-1]
    if (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.js")
        or name.endswith(".test.ts")
        or name.endswith(".spec.js")
        or name.endswith(".spec.ts")
        or "/tests/" in p
        or "/test/" in p
    ):
        return "test"
    if p.endswith((".md", ".markdown", ".rst")) or "/docs/" in p or "doc" in p:
        return "docs"
    if name.endswith(_CONFIG_EXT) or "/config/" in p or name in ("dockerfile", "makefile"):
        return "config"
    if "log" in p or p.endswith(".log"):
        return "log"
    return "code"


def parse_diffstat(patch: str) -> list[dict]:
    """Minimal unified-diff parser → [{path, added, removed}, ...]."""
    files: list[dict] = []
    cur: dict | None = None

    def _path_from(line: str) -> str:
        m = re.search(r"b/(.+)$", line)
        return m.group(1) if m else line.strip()

    for line in (patch or "").splitlines():
        if line.startswith("diff --git"):
            if cur:
                files.append(cur)
            cur = {"path": _path_from(line), "added": 0, "removed": 0}
        elif line.startswith("+++"):
            if cur is None:
                cur = {"path": _path_from(line), "added": 0, "removed": 0}
        elif line.startswith("+"):
            if cur and not line.startswith("+++"):
                cur["added"] += 1
        elif line.startswith("-"):
            if cur and not line.startswith("---"):
                cur["removed"] += 1
    if cur:
        files.append(cur)
    return files


def _blast_radius(rep: dict, fixed_files: set[str]) -> list[str]:
    br: list[str] = []
    if rep.get("patch"):
        br = [f["path"] for f in parse_diffstat(rep["patch"])]
    if not br:
        br = [e.get("path") for e in (rep.get("evidence") or []) if e.get("path")]
    if not br:
        br = list(rep.get("files_examined") or [])
    seen: set[str] = set()
    out: list[str] = []
    for p in br:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out[:15]


def enrich_report(rep: dict) -> dict:
    """Return ``rep`` with missing enrichment fields filled. Idempotent."""
    if not isinstance(rep, dict):
        return rep

    lang = detect_lang(rep)
    evidence = rep.get("evidence") or []
    proposed = rep.get("proposed_changes") or []
    patch = rep.get("patch") or ""
    tests = rep.get("tests") or []
    conf = rep.get("confidence", "medium")

    fixed_files = {f["path"] for f in parse_diffstat(patch)} if patch else set()

    # ── evidence strength / kind / claim ──
    for e in evidence:
        if not isinstance(e, dict):
            continue
        if "kind" not in e:
            e["kind"] = infer_kind(e.get("path", ""))
        if "strength" not in e:
            e["strength"] = "strong" if e.get("path") in fixed_files else "moderate"
        if "claim" not in e:
            e["claim"] = rep.get("root_cause", "") or None

    # ── impact (blast radius + severity/likelihood) ──
    if "impact" not in rep or rep.get("impact") is None:
        br = _blast_radius(rep, fixed_files)
        severity = "high" if len(br) >= 5 else ("medium" if len(br) >= 2 else "low")
        rep["impact"] = {
            "severity": severity,
            "likelihood": "medium",
            "blast_radius": br,
        }

    # ── confidence rationale ──
    if not rep.get("confidence_rationale"):
        n_ev = len(evidence)
        n_files = len({e.get("path") for e in evidence if e.get("path")})
        has_fix = bool(patch) or len(proposed) > 0
        has_tests = len(tests) > 0
        if lang == "zh":
            fix_note = (
                "（含补丁与测试）"
                if (patch and has_tests)
                else "（含测试）"
                if has_tests
                else "（含补丁）"
                if patch
                else ""
            )
            rep["confidence_rationale"] = (
                f"置信度为「{conf}」，基于 {n_ev} 条证据（覆盖 {n_files} 个文件）"
                f"锁定根因{'，并已提出修复方案' + fix_note if has_fix else ''}。"
            )
        else:
            fix_note = (
                " with patch and tests"
                if (patch and has_tests)
                else " with tests"
                if has_tests
                else " with patch"
                if patch
                else ""
            )
            rep["confidence_rationale"] = (
                f"Confidence is '{conf}', based on {n_ev} pieces of evidence across "
                f"{n_files} files to pin down the root cause{', and a fix is proposed' + fix_note if has_fix else ''}."
            )

    # ── fix rationale ──
    if not rep.get("fix_rationale"):
        rc = (rep.get("root_cause", "") or "")[:60]
        n_ch = len(proposed)
        if lang == "zh":
            rep["fix_rationale"] = (
                f"针对根因「{rc}…」提出 {n_ch} 处修改，从根上消除问题成因。"
            )
        else:
            rep["fix_rationale"] = (
                f"Targeting the root cause ('{rc}...'), {n_ch} change(s) are proposed "
                f"to remove the underlying cause."
            )

    # ── hypotheses: accepted root cause only (no fabricated rejections) ──
    if "hypotheses" not in rep or not rep.get("hypotheses"):
        reason0 = (
            evidence[0].get("reason", "")
            if evidence and isinstance(evidence[0], dict)
            else ""
        )
        rep["hypotheses"] = [
            {
                "statement": rep.get("root_cause", ""),
                "status": "accepted",
                "rationale": reason0 or "主要结论，由上述证据支撑",
            }
        ]

    # reproduction is intentionally left unset (cannot be derived honestly).
    return rep


def enrich_report_json(report_json: str) -> str:
    """Convenience wrapper operating on a JSON string."""
    rep = json.loads(report_json)
    return json.dumps(enrich_report(rep), ensure_ascii=False)
