import re
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

ConfidenceLevel = Literal["low", "medium", "high"]
SessionStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
ReviewStatus = Literal["not_run", "approved", "revised", "unavailable"]

_LINES_PATTERN = re.compile(r"L\d+(?:-L?\d+)?")


class AnalyzeRequest(BaseModel):
    issue_url: HttpUrl


class StreamRequest(BaseModel):
    issue_url: HttpUrl | None = None
    session_id: str | None = None
    # 应用内设置覆盖：仅当显式提供时才覆盖服务端环境变量默认值
    language: Literal["zh", "en"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)
    thinking: Literal["enabled", "disabled"] | None = None
    reasoning_effort: Literal["high", "max"] | None = None
    review: bool | None = None

    @model_validator(mode="after")
    def _require_issue_or_session(self) -> "StreamRequest":
        if self.issue_url is None and self.session_id is None:
            raise ValueError("issue_url or session_id is required")
        return self


class CodeReference(BaseModel):
    path: str
    lines: str | None = Field(default=None, description="Line or range in L12 or L12-L18 format")
    reason: str | None = Field(default=None, description="Why this evidence supports the root cause")
    # 证据强度与类型：用于前端「证据强度图」按可信度可视化，并支持点击下钻到对应条目
    strength: Literal["weak", "moderate", "strong"] = "moderate"
    kind: Literal["code", "log", "test", "config", "docs"] = "code"
    # 该证据支撑的是哪一条结论（根因/影响/修复），缺省时视为支持整体根因
    claim: str | None = Field(default=None, description="Which conclusion this evidence supports")

    @field_validator("lines")
    @classmethod
    def _validate_lines_format(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not _LINES_PATTERN.fullmatch(value):
            raise ValueError("lines must use 'L12' or 'L12-L18' format")
        return value


class EvidenceAudit(BaseModel):
    valid_references: int = 0
    root_cause_supported: bool = False


class ReviewAudit(BaseModel):
    status: ReviewStatus = "not_run"
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    reviewer_model: str | None = None


class Hypothesis(BaseModel):
    """调查过程中提出过的备选解释。呈现实则拒绝的假设，是「论证严谨」的可视信号。"""

    statement: str
    status: Literal["accepted", "rejected", "open"] = "open"
    rationale: str = ""


class Impact(BaseModel):
    """问题影响面：严重度、发生可能性、波及的模块/文件（用于「波及范围图」）。"""

    severity: Literal["low", "medium", "high", "critical"] = "medium"
    likelihood: Literal["low", "medium", "high"] = "medium"
    blast_radius: list[str] = Field(default_factory=list)


class Reproduction(BaseModel):
    """复现路径：让结论可被第三方独立验证，是报告可信度的关键支撑。"""

    steps: list[str] = Field(default_factory=list)
    observed: str = ""
    expected: str = ""


class AnalysisReport(BaseModel):
    summary: str
    root_cause: str
    confidence: ConfidenceLevel
    evidence: list[CodeReference]
    proposed_changes: list[str]
    patch: str | None = Field(default=None, description="Unified diff patch for the fix")
    tests: list[str]
    risks: list[str]
    files_examined: list[str] = Field(default_factory=list)
    evidence_audit: EvidenceAudit = Field(default_factory=EvidenceAudit)
    review_audit: ReviewAudit = Field(default_factory=ReviewAudit)
    # ── 增强字段：让报告更具说服力、图表更具信息量（全部可选，向后兼容旧报告）──
    confidence_rationale: str = Field(
        default="", description="Explanation of confidence level"
    )
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    impact: Impact | None = Field(default=None, description="Severity, likelihood, blast radius")
    reproduction: Reproduction | None = Field(default=None, description="Reproduction steps")
    fix_rationale: str = Field(
        default="", description="Reason for choosing this fix"
    )


class ReviewOutcome(BaseModel):
    verdict: Literal["approved", "revised"]
    summary: str
    findings: list[str] = Field(default_factory=list)
    report: AnalysisReport


class IssueData(BaseModel):
    owner: str
    repo: str
    number: int
    title: str
    body: str
    labels: list[str]
    comments: list[str]
    default_branch: str
    head_sha: str = ""


class SourceFile(BaseModel):
    path: str
    content: str


class ChatRequest(BaseModel):
    session_id: str | None = None
    issue_url: HttpUrl | None = None
    message: str = Field(min_length=1, max_length=32_000)
    # 应用内设置覆盖
    language: Literal["zh", "en"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)
    thinking: Literal["enabled", "disabled"] | None = None
    reasoning_effort: Literal["high", "max"] | None = None
    review: bool | None = None
    # 重新生成：忽略上一条 assistant 回复，基于最近一条 user 消息重新请求
    regenerate: bool = False


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tools_used: list[str] = Field(default_factory=list)
    report: AnalysisReport | None = None


class CreatePRResponse(BaseModel):
    pr_url: str
    branch: str


class ApplyFixRequest(BaseModel):
    confirm: bool = Field(default=False)


class SessionSummary(BaseModel):
    session_id: str
    issue_url: str
    owner: str = ""
    repo: str = ""
    issue_number: int | None = None
    title: str
    status: SessionStatus
    phase: str = "queued"
    error_message: str | None = None
    archived: bool = False
    version: int = 0
    metrics: dict[str, int | float] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    head_sha: str = ""


class SessionEventRecord(BaseModel):
    sequence: int
    type: str
    data: dict[str, Any] | None = None
    message: str = ""
    created_at: str


class SessionDetail(SessionSummary):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    report: AnalysisReport | None = None
    events: list[SessionEventRecord] = Field(default_factory=list)


class SessionUpdateRequest(BaseModel):
    display_title: str | None = Field(default=None, max_length=160)
    archived: bool | None = None

    @field_validator("display_title")
    @classmethod
    def _normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("display_title cannot be empty")
        return normalized
