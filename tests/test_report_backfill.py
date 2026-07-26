from __future__ import annotations

import copy
import json

import pytest

from app import report_backfill


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("", "root"),
        ("/", "/"),
        ("auth", "auth"),
        ("src/auth/client.py", "auth"),
        (r"app\payments\checkout.py", "payments"),
        ("tests/unit/auth/test_login.py", "unit/auth"),
        ("standalone.py", "standalone.py"),
    ],
)
def test_module_of_normalizes_paths(path: str, expected: str) -> None:
    assert report_backfill._module_of(path) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_auth.py", "test"),
        ("src/auth.spec.ts", "test"),
        ("docs/setup.rst", "docs"),
        ("src/document.py", "code"),
        ("config/app.toml", "config"),
        ("Dockerfile", "config"),
        ("logs/server.log", "log"),
        ("src/service.py", "code"),
    ],
)
def test_infer_kind_uses_file_and_directory_signals(path: str, expected: str) -> None:
    assert report_backfill.infer_kind(path) == expected


def test_detect_lang_uses_report_content() -> None:
    assert report_backfill.detect_lang({"summary": "修复认证问题"}) == "zh"
    assert report_backfill.detect_lang({"summary": "Fix authentication failure"}) == "en"


def test_parse_diffstat_handles_multiple_files_and_headerless_diff() -> None:
    patch = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,2 +1,2 @@
-old
+new
diff --git a/tests/test_a.py b/tests/test_a.py
--- a/tests/test_a.py
+++ b/tests/test_a.py
+assert fixed
"""
    assert report_backfill.parse_diffstat(patch) == [
        {"path": "src/a.py", "added": 1, "removed": 1},
        {"path": "tests/test_a.py", "added": 1, "removed": 0},
    ]
    assert report_backfill.parse_diffstat("+++ b/src/new.py\n+created\n") == [
        {"path": "src/new.py", "added": 1, "removed": 0}
    ]
    assert report_backfill.parse_diffstat("") == []


def test_enrich_report_derives_english_patch_fields_and_is_idempotent() -> None:
    report = {
        "summary": "Digest authentication fails",
        "root_cause": "Bytes credentials are formatted as repr strings.",
        "confidence": "high",
        "evidence": [{"path": "src/auth/digest.py", "lines": "L20-L24", "reason": "Formats bytes directly."}],
        "proposed_changes": ["Decode credentials before hashing."],
        "patch": "diff --git a/src/auth/digest.py b/src/auth/digest.py\n-old\n+new\n",
        "tests": ["Covers non-ASCII bytes credentials."],
    }

    enriched = report_backfill.enrich_report(report)

    assert enriched is report
    assert enriched["evidence"][0] == {
        "path": "src/auth/digest.py",
        "lines": "L20-L24",
        "reason": "Formats bytes directly.",
        "kind": "code",
        "strength": "strong",
        "claim": report["root_cause"],
    }
    assert enriched["impact"] == {
        "severity": "high",
        "likelihood": "medium",
        "blast_radius": ["auth"],
    }
    assert "with patch and tests" in enriched["confidence_rationale"]
    assert "1 change(s)" in enriched["fix_rationale"]
    assert enriched["hypotheses"][0]["status"] == "accepted"
    assert enriched["hypotheses"][0]["rationale"] == "Formats bytes directly."

    snapshot = copy.deepcopy(enriched)
    assert report_backfill.enrich_report(enriched) == snapshot


def test_enrich_report_derives_chinese_evidence_only_fields() -> None:
    report = {
        "summary": "文档配置说明过时",
        "root_cause": "文档仍引用旧配置项",
        "confidence": "medium",
        "evidence": [{"path": "docs/config.md", "reason": "配置名与代码不一致"}],
        "files_examined": ["docs/config.md"],
        "proposed_changes": [],
        "tests": [],
    }

    enriched = report_backfill.enrich_report(report)

    assert enriched["evidence"][0]["kind"] == "docs"
    assert enriched["evidence"][0]["strength"] == "moderate"
    assert enriched["impact"]["severity"] == "low"
    assert enriched["impact"]["blast_radius"] == ["docs"]
    assert "置信度为「medium」" in enriched["confidence_rationale"]
    assert "提出 0 处修改" in enriched["fix_rationale"]
    assert enriched["hypotheses"][0]["rationale"] == "配置名与代码不一致"
    assert "reproduction" not in enriched


def test_enrich_report_normalizes_existing_impact_without_overwriting_fields() -> None:
    report = {
        "summary": "Authentication issue",
        "root_cause": "Token validation is bypassed.",
        "confidence": "high",
        "confidence_rationale": "Existing confidence rationale.",
        "fix_rationale": "Existing fix rationale.",
        "evidence": [{"path": "src/security/token.py", "kind": "config", "strength": "weak", "claim": "Existing"}],
        "patch": "diff --git a/src/security/token.py b/src/security/token.py\n-old\n+new\n",
        "impact": {"severity": "unknown", "likelihood": "high", "blast_radius": ["src/security/token.py", "security"]},
        "hypotheses": [{"statement": "Existing", "status": "rejected", "rationale": "Already reviewed"}],
    }

    enriched = report_backfill.enrich_report(report)

    assert enriched["impact"] == {"severity": "high", "likelihood": "high", "blast_radius": ["security"]}
    assert enriched["confidence_rationale"] == "Existing confidence rationale."
    assert enriched["fix_rationale"] == "Existing fix rationale."
    assert enriched["hypotheses"][0]["status"] == "rejected"
    assert enriched["evidence"][0]["kind"] == "config"
    assert enriched["evidence"][0]["strength"] == "weak"
    assert enriched["evidence"][0]["claim"] == "Existing"


def test_enrich_report_falls_back_to_files_examined_and_default_hypothesis_reason() -> None:
    report = {
        "summary": "Runtime failure",
        "root_cause": "A default handler is missing.",
        "evidence": [],
        "files_examined": ["src/runtime/handler.py"],
        "impact": {"severity": "low", "likelihood": "medium", "blast_radius": []},
    }

    enriched = report_backfill.enrich_report(report)

    assert enriched["impact"]["blast_radius"] == ["runtime"]
    assert enriched["impact"]["severity"] == "medium"
    assert enriched["hypotheses"][0]["rationale"] == "主要结论，由上述证据支撑"


def test_enrich_report_json_round_trips_unicode_and_rejects_invalid_json() -> None:
    source = json.dumps({"summary": "修复错误", "root_cause": "空值未处理", "evidence": []}, ensure_ascii=False)
    enriched = json.loads(report_backfill.enrich_report_json(source))
    assert enriched["summary"] == "修复错误"
    assert enriched["impact"]["severity"] == "medium"

    with pytest.raises(json.JSONDecodeError):
        report_backfill.enrich_report_json("not-json")


def test_enrich_report_returns_non_mapping_unchanged() -> None:
    value = ["not", "a", "report"]
    assert report_backfill.enrich_report(value) is value  # type: ignore[arg-type]
