from app.evidence import EvidenceValidator, extract_cited_text, extract_identifiers, parse_line_range
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


def test_extract_cited_text_out_of_range() -> None:
    assert extract_cited_text("only\none\n", "L9-L10") == ""
    assert parse_line_range("not-a-range") is None


def test_invalid_line_format_dropped() -> None:
    item = CodeReference.model_construct(
        path="src/a.py", lines="9-12", reason="x", strength="moderate", kind="code", claim=None
    )
    report = _report(evidence=[item], root_cause="root")
    validated = EvidenceValidator().validate(
        report,
        files_read=["src/a.py"],
        line_counts={"src/a.py": 10},
        file_cache={"src/a.py": "def foo():\n    return 1\n"},
    )
    assert validated.evidence == []
    assert validated.confidence == "low"


def test_stopwords_only_reason_does_not_downgrade_via_identifiers() -> None:
    report = _report(
        evidence=[
            CodeReference(path="src/a.py", lines="L1", reason="the value returns error", strength="strong"),
            CodeReference(path="src/b.py", lines="L1", reason="the value returns error", strength="strong"),
            CodeReference(path="src/c.py", lines="L1", reason="the value returns error", strength="strong"),
        ],
        root_cause="the file returns error",
    )
    file_cache = {
        "src/a.py": "pass\n",
        "src/b.py": "pass\n",
        "src/c.py": "pass\n",
    }
    validated = EvidenceValidator().validate(
        report,
        files_read=["src/a.py", "src/b.py", "src/c.py"],
        line_counts={"src/a.py": 1, "src/b.py": 1, "src/c.py": 1},
        file_cache=file_cache,
    )
    # 无有效标识符 → unknown，不因 weak 全灭而压置信度；条数 ≥3 仍可 high
    assert validated.confidence == "high"


def test_empty_cited_text_marks_weak() -> None:
    report = _report(
        evidence=[
            CodeReference(path="src/a.py", lines="L1-L1", reason="login_handler", strength="strong"),
            CodeReference(path="src/b.py", lines="L1-L1", reason="login_handler", strength="strong"),
            CodeReference(path="src/c.py", lines="L1-L1", reason="login_handler", strength="strong"),
        ],
        root_cause="login_handler bug",
    )
    file_cache = {"src/a.py": "", "src/b.py": "", "src/c.py": ""}
    validated = EvidenceValidator().validate(
        report,
        files_read=["src/a.py", "src/b.py", "src/c.py"],
        line_counts={"src/a.py": 1, "src/b.py": 1, "src/c.py": 1},
        file_cache=file_cache,
    )
    assert all(item.strength == "weak" for item in validated.evidence)
    assert validated.confidence == "medium"


def test_extract_identifiers_keeps_short_uppercase() -> None:
    tokens = extract_identifiers("see ID and parse_date")
    assert "ID" in tokens
    assert "parse_date" in tokens
