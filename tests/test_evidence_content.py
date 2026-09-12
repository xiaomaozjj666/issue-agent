from app.evidence import (
    EvidenceValidator,
    extract_cited_text,
    extract_identifiers,
    parse_line_range,
)
from app.models import AnalysisReport, CodeReference


def _report(*, evidence: list[CodeReference], root_cause: str, confidence: str = "high") -> AnalysisReport:
    return AnalysisReport(
        summary="s",
        root_cause=root_cause,
        confidence=confidence,  # type: ignore[arg-type]
        evidence=evidence,
        proposed_changes=["fix"],
        tests=["t"],
        risks=[],
    )


def test_parse_line_range_and_extract_cited_text() -> None:
    content = "alpha\nbeta\ngamma\ndelta"
    assert parse_line_range("L2-L3") == (2, 3)
    assert extract_cited_text(content, "L2-L3") == "beta\ngamma"
    assert extract_cited_text(content, None).startswith("alpha")


def test_extract_identifiers_filters_stopwords() -> None:
    tokens = extract_identifiers("the parse_date function returns error because value is null")
    assert "parse_date" in tokens
    assert "the" not in tokens
    assert "error" not in tokens


def test_content_alignment_downgrades_mismatched_evidence() -> None:
    report = _report(
        evidence=[
            CodeReference(
                path="src/auth.py",
                lines="L1-L2",
                reason="login_handler forgot to check password_hash",
                strength="strong",
            )
        ],
        root_cause="login_handler mishandles password_hash",
    )
    file_cache = {"src/auth.py": "def other_module():\n    return 1\n"}
    validated = EvidenceValidator().validate(
        report,
        files_read=["src/auth.py"],
        line_counts={"src/auth.py": 2},
        file_cache=file_cache,
    )
    assert len(validated.evidence) == 1
    assert validated.evidence[0].strength == "weak"
    # 全部内容对齐失败时，high 被压到 medium
    assert validated.confidence == "medium"


def test_content_alignment_keeps_matching_evidence_strong() -> None:
    report = _report(
        evidence=[
            CodeReference(
                path="src/auth.py",
                lines="L1-L2",
                reason="login_handler forgot password_hash",
                strength="strong",
            )
        ],
        root_cause="login_handler mishandles password_hash",
    )
    file_cache = {"src/auth.py": "def login_handler(password_hash):\n    return password_hash\n"}
    validated = EvidenceValidator().validate(
        report,
        files_read=["src/auth.py"],
        line_counts={"src/auth.py": 2},
        file_cache=file_cache,
    )
    # 内容对齐：强度保持 strong；证据条数不足 3 时 high 仍按原有规则压到 medium
    assert validated.evidence[0].strength == "strong"
    assert validated.confidence == "medium"


def test_three_aligned_evidence_allow_high_confidence() -> None:
    evidence = [
        CodeReference(
            path="src/auth.py",
            lines=f"L{i}-L{i}",
            reason="login_handler forgot password_hash",
            strength="strong",
        )
        for i in (1, 2, 3)
    ]
    report = _report(
        evidence=evidence,
        root_cause="login_handler mishandles password_hash",
    )
    file_cache = {
        "src/auth.py": "def login_handler(password_hash):\n    return password_hash\n# password_hash\n"
    }
    validated = EvidenceValidator().validate(
        report,
        files_read=["src/auth.py"],
        line_counts={"src/auth.py": 3},
        file_cache=file_cache,
    )
    assert all(item.strength == "strong" for item in validated.evidence)
    assert validated.confidence == "high"


def test_without_file_cache_keeps_legacy_behaviour() -> None:
    report = _report(
        evidence=[
            CodeReference(path="src/a.py", lines="L1", reason="x", strength="strong"),
            CodeReference(path="src/b.py", lines="L1", reason="y", strength="strong"),
            CodeReference(path="src/c.py", lines="L1", reason="z", strength="strong"),
        ],
        root_cause="root",
    )
    validated = EvidenceValidator().validate(
        report,
        files_read=["src/a.py", "src/b.py", "src/c.py"],
        line_counts={"src/a.py": 1, "src/b.py": 1, "src/c.py": 1},
    )
    assert all(item.strength == "strong" for item in validated.evidence)
    assert validated.confidence == "high"
