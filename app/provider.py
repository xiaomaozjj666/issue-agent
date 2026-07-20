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
    thinking: ThinkingMode | None = None,
) -> dict:
    """Build model and reasoning options without leaking provider quirks across agents.

    ``thinking`` overrides the configured thinking mode for this single call. Use
    ``thinking="disabled"`` for structured-output calls (e.g. report generation)
    so the reasoning budget does not consume the ``max_tokens`` reserved for the
    final JSON content.
    """
    options: dict = {"model": model or settings.openai_model}
    effective_thinking = settings.openai_thinking if thinking is None else thinking
    if is_deepseek(settings):
        options["extra_body"] = {"thinking": {"type": effective_thinking}}
        if effective_thinking == "enabled":
            options["reasoning_effort"] = settings.openai_reasoning_effort
        elif temperature is not None:
            options["temperature"] = temperature
    elif temperature is not None:
        options["temperature"] = temperature
    return options
