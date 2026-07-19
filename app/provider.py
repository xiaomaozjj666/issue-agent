"""Provider-specific request options for OpenAI-compatible chat APIs."""

from typing import Literal
from urllib.parse import urlparse

from app.config import Settings

ThinkingMode = Literal["enabled", "disabled"]


def is_deepseek(settings: Settings) -> bool:
    """Return whether the configured endpoint is the official DeepSeek API."""
    hostname = (urlparse(settings.openai_base_url).hostname or "").casefold()
    return hostname == "api.deepseek.com" or hostname.endswith(".api.deepseek.com")


def chat_request_options(
    settings: Settings,
    *,
    model: str | None = None,
    temperature: float | None = 0.1,
) -> dict:
    """Build model and reasoning options without leaking provider quirks across agents."""
    options: dict = {"model": model or settings.openai_model}
    if is_deepseek(settings):
        options["extra_body"] = {"thinking": {"type": settings.openai_thinking}}
        if settings.openai_thinking == "enabled":
            options["reasoning_effort"] = settings.openai_reasoning_effort
        elif temperature is not None:
            options["temperature"] = temperature
    elif temperature is not None:
        options["temperature"] = temperature
    return options
