from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = Field(min_length=1)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"
    openai_timeout: float = Field(default=60.0, gt=0, le=300)
    openai_max_retries: int = Field(default=2, ge=0, le=5)
    github_token: str | None = None
    github_max_file_bytes: int = Field(default=512_000, ge=4_096, le=2_000_000)
    max_candidate_files: int = Field(default=12, ge=1, le=30)
    max_planning_paths: int = Field(default=80, ge=10, le=200)
    max_file_chars: int = Field(default=16_000, ge=1_000, le=50_000)
    max_total_context_chars: int = Field(default=80_000, ge=5_000, le=200_000)
    max_output_tokens: int = Field(default=4_000, ge=500, le=8_000)
    max_agent_iterations: int = Field(default=15, ge=3, le=40)
    max_chat_tokens: int = Field(default=2_000, ge=500, le=8_000)

    # --- New settings for v0.3.0 ---
    language: str = Field(default="zh", pattern=r"^(zh|en)$")
    api_key: str | None = None
    write_mode: bool = False
    session_db_path: str = ":memory:"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
