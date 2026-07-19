"""Deterministic validation for model-generated investigation evidence."""

import re
from typing import Literal

from app.i18n import t
from app.models import AnalysisReport

LINE_RANGE = re.compile(r"^L(\d+)(?:-L?(\d+))?$")


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
    ) -> AnalysisReport:
        read_paths = set(files_read)
        report.evidence = [
            item
            for item in report.evidence
            if item.path in read_paths and self.has_valid_lines(item.lines, line_counts.get(item.path, 0))
        ]
        report.files_examined = files_read
        report.evidence_audit.valid_references = len(report.evidence)
        report.evidence_audit.root_cause_supported = bool(report.evidence) and all(
            bool(item.reason and item.reason.strip()) for item in report.evidence
        )

        reference_count = len(report.evidence)
        confidence_rank = {"low": 0, "medium": 1, "high": 2}
        maximum_confidence: Literal["low", "medium", "high"] = (
            "low" if reference_count == 0 else "medium" if reference_count < 3 else "high"
        )
        if confidence_rank[report.confidence] > confidence_rank[maximum_confidence]:
            report.confidence = maximum_confidence

        if not report.evidence_audit.root_cause_supported:
            report.confidence = "low"
            warning = t("evidence_unsupported")
            if warning not in report.risks:
                report.risks.append(warning)
        return report
