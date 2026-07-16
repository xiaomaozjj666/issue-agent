import re
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

ConfidenceLevel = Literal["low", "medium", "high"]
SessionStatus = Literal["queued", "running", "completed", "failed", "cancelled"]

_LINES_PATTERN = re.compile(r"L\d+(?:-L?\d+)?")


class AnalyzeRequest(BaseModel):
    issue_url: HttpUrl


class StreamRequest(BaseModel):
    issue_url: HttpUrl | None = None
    session_id: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def _require_issue_or_session(self) -> "StreamRequest":
        if self.issue_url is None and self.session_id is None:
            raise ValueError("issue_url or session_id is required")
        return self


class CodeReference(BaseModel):
    path: str
    lines: str | None = Field(default=None, description="Line or range in L12 or L12-L18 format")
    reason: str | None = Field(default=None, description="Why this evidence supports the root cause")

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


class IssueData(BaseModel):
    owner: str
    repo: str
    number: int
    title: str
    body: str
    labels: list[str]
    comments: list[str]
    default_branch: str


class SourceFile(BaseModel):
    path: str
    content: str


class ChatRequest(BaseModel):
    session_id: str | None = None
    issue_url: HttpUrl | None = None
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tools_used: list[str] = Field(default_factory=list)
    report: AnalysisReport | None = None


class CreatePRRequest(BaseModel):
    session_id: str
    branch: str
    title: str
    body: str
    changes: list[dict] = Field(default_factory=list)


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
