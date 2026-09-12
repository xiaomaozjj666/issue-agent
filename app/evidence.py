"""Deterministic validation for model-generated investigation evidence."""

import re
from typing import Literal

from app.i18n import t
from app.models import AnalysisReport, CodeReference

LINE_RANGE = re.compile(r"^L(\d+)(?:-L?(\d+))?$")
# 英文标识符（函数名、变量、异常名等）。中文说明里通常混有代码符号，这里只抽英文 token。
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{1,}\b")
# 过滤过泛的英文词，避免“the file returns error”一类词误判为相关
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "into",
    "when",
    "where",
    "what",
    "which",
    "will",
    "must",
    "should",
    "would",
    "could",
    "does",
    "not",
    "none",
    "null",
    "true",
    "false",
    "error",
    "value",
    "result",
    "return",
    "returns",
    "function",
    "method",
    "class",
    "object",
    "string",
    "number",
    "integer",
    "boolean",
    "list",
    "dict",
    "file",
    "path",
    "line",
    "lines",
    "code",
    "test",
    "tests",
    "issue",
    "root",
    "cause",
    "because",
    "since",
    "after",
    "before",
    "under",
    "over",
    "only",
    "also",
    "then",
    "than",
    "via",
    "using",
    "used",
    "use",
    "get",
    "set",
    "add",
    "run",
    "new",
    "old",
    "all",
    "any",
    "one",
    "two",
}


def extract_identifiers(text: str) -> set[str]:
    """Pull candidate code identifiers from free text (English tokens only)."""
    tokens = set()
    for match in _IDENT_RE.finditer(text or ""):
        token = match.group(0)
        if token.lower() in _STOPWORDS:
            continue
        # 全大写常量过短没有区分度时仍保留；纯数字已由 regex 排除
        if len(token) < 3 and not token.isupper():
            continue
        tokens.add(token)
        tokens.add(token.lower())
    return tokens


def parse_line_range(lines: str | None) -> tuple[int, int] | None:
    if not lines:
        return None
    match = LINE_RANGE.fullmatch(lines)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2) or start)
    return start, end


def extract_cited_text(content: str, lines: str | None, *, max_chars: int = 4_000) -> str:
    """Return the source text covered by a L-range citation, or a bounded prefix."""
    source_lines = content.splitlines()
    parsed = parse_line_range(lines)
    if parsed is None:
        return "\n".join(source_lines)[:max_chars]
    start, end = parsed
    if start < 1 or start > len(source_lines):
        return ""
    end = min(end, len(source_lines))
    return "\n".join(source_lines[start - 1 : end])[:max_chars]


def _has_identifier_overlap(query_tokens: set[str], cited_text: str) -> bool:
    if not query_tokens or not cited_text:
        return False
    cited_lower = cited_text.lower()
    # 直接子串匹配：标识符在源码中通常以原样出现（含调用、赋值、import）
    return any(token in cited_lower if token.islower() else token in cited_text for token in query_tokens)


def _score_content_alignment(
    item: CodeReference, root_cause: str, file_content: str | None
) -> Literal["aligned", "weak", "unknown"]:
    """Compare evidence reason/root-cause identifiers against the cited source lines."""
    if file_content is None:
        return "unknown"
    if not item.lines:
        # 未指定行号时只能确认文件被读过，内容对齐证据不足
        return "unknown"
    cited = extract_cited_text(file_content, item.lines)
    if not cited.strip():
        return "weak"
    tokens = extract_identifiers(f"{item.reason or ''} {root_cause}")
    if not tokens:
        return "unknown"
    return "aligned" if _has_identifier_overlap(tokens, cited) else "weak"


class EvidenceValidator:
    """Constrain report confidence to evidence the agent actually inspected."""

    @staticmethod
    def has_valid_lines(lines: str | None, line_count: int) -> bool:
        if lines is None:
            return True
        match = LINE_RANGE.fullmatch(lines)
        if not match:
            return False
        start = int(match.group(1))
        end = int(match.group(2) or start)
        return 1 <= start <= end <= line_count

    def validate(
        self,
        report: AnalysisReport,
        *,
        files_read: list[str],
        line_counts: dict[str, int],
        file_cache: dict[str, str] | None = None,
    ) -> AnalysisReport:
        read_paths = set(files_read)
        kept: list[CodeReference] = []
        aligned_count = 0
        weak_count = 0
        for item in report.evidence:
            if item.path not in read_paths:
                continue
            if not self.has_valid_lines(item.lines, line_counts.get(item.path, 0)):
                continue
            alignment = _score_content_alignment(item, report.root_cause, (file_cache or {}).get(item.path))
            if alignment == "aligned":
                aligned_count += 1
            elif alignment == "weak":
                # 路径与行号合法，但引用行内容撑不住结论：降级强度，不直接丢弃
                weak_count += 1
                item.strength = "weak"
            kept.append(item)
        report.evidence = kept
        report.files_examined = files_read
        report.evidence_audit.valid_references = len(report.evidence)
        report.evidence_audit.root_cause_supported = bool(report.evidence) and all(
            bool(item.reason and item.reason.strip()) for item in report.evidence
        )

        reference_count = len(report.evidence)
        confidence_rank = {"low": 0, "medium": 1, "high": 2}
        # 上限规则：0 条 → low；不足 3 条，或全部引用内容与结论无交集 → medium；否则 high
        all_content_weak = (
            file_cache is not None and reference_count > 0 and aligned_count == 0 and weak_count == reference_count
        )
        if reference_count == 0:
            maximum_confidence: Literal["low", "medium", "high"] = "low"
        elif all_content_weak or reference_count < 3:
            maximum_confidence = "medium"
        else:
            maximum_confidence = "high"
        if confidence_rank[report.confidence] > confidence_rank[maximum_confidence]:
            report.confidence = maximum_confidence

        if not report.evidence_audit.root_cause_supported:
            report.confidence = "low"
            warning = t("evidence_unsupported")
            if warning not in report.risks:
                report.risks.append(warning)
        return report
