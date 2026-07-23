"""Application configuration via environment variables.

All runtime-tunable knobs are exposed as pydantic-settings fields so they can
be overridden through ``.env`` or process environment variables without code
changes.  The ``Settings`` instance is frozen after creation to prevent
accidental mutation at runtime.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the Issue Agent application.

    Fields are grouped by subsystem: LLM provider, GitHub integration,
    agent behaviour, review, and runtime behaviour.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    # ── LLM provider ─────────────────────────────────────────────────
    openai_api_key: str = Field(min_length=1)
    openai_base_url: str = "https://api.deepseek.com"
    openai_model: str = Field(default="deepseek-v4-pro", min_length=1)
    openai_thinking: Literal["enabled", "disabled"] = "enabled"
    openai_reasoning_effort: Literal["high", "max"] = "high"
    openai_timeout: float = Field(default=180.0, gt=0, le=300, description="DeepSeek thinking mode often needs 60-120s")
    openai_max_retries: int = Field(default=2, ge=0, le=5)

    # ── GitHub integration ───────────────────────────────────────────
    github_token: str | None = None
    github_timeout: float = Field(default=30.0, gt=0, le=120, description="HTTP timeout for GitHub API calls")
    github_max_retries: int = Field(default=3, ge=0, le=5, description="App-level retries for transient GitHub errors")
    github_max_file_bytes: int = Field(default=512_000, ge=4_096, le=2_000_000)

    # ── Tool execution ───────────────────────────────────────────────
    # 单个工具调用（如 search_code/grep_content 调用 GitHub API）的超时上限。
    # 防止网络抖动或 GitHub 慢响应导致整个 stream 卡住，前端 SSE 无事件输出。
    tool_timeout: float = Field(default=60.0, gt=0, le=180, description="Per-tool execution timeout")

    # ── Agent behaviour ──────────────────────────────────────────────
    max_candidate_files: int = Field(default=20, ge=1, le=30)
    max_planning_paths: int = Field(default=80, ge=10, le=200)
    max_file_chars: int = Field(default=20_000, ge=1_000, le=50_000)
    max_total_context_chars: int = Field(default=100_000, ge=5_000, le=200_000)
    max_output_tokens: int = Field(default=8_000, ge=500, le=16_000)
    max_agent_iterations: int = Field(default=15, ge=3, le=40)
    max_investigation_ledger_chars: int = Field(default=12_000, ge=1_000, le=50_000)
    max_chat_tokens: int = Field(default=2_000, ge=500, le=16_000)

    # ── Independent review ───────────────────────────────────────────
    independent_review: bool = True
    review_model: str | None = None
    review_max_tokens: int = Field(default=8_000, ge=500, le=16_000)
    # 报告生成/审查的重试次数（含首次）：第 1 次用原配置，中间几次带错误反馈保留 thinking，
    # 最后一次降级 thinking disabled 保底。默认 3 次：原配置 → 带反馈重试 → 降级保底。
    max_report_retries: int = Field(default=3, ge=1, le=5)
    max_review_context_chars: int = Field(default=32_000, ge=4_000, le=100_000)

    # ── Runtime behaviour ────────────────────────────────────────────
    language: str = Field(default="zh", pattern=r"^(zh|en)$")
    api_key: str | None = None
    write_mode: bool = False
    session_db_path: str = "data/sessions.db"
    session_stale_after_seconds: int = Field(default=1800, ge=60, le=86_400)
    session_retention_days: int = Field(
        default=30, ge=1, le=365, description="Auto-purge completed sessions older than this"
    )
    max_pr_files: int = Field(default=20, ge=1, le=50)
    max_pr_total_bytes: int = Field(default=1_000_000, ge=4_096, le=10_000_000)


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()  # type: ignore[call-arg]
