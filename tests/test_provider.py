from types import SimpleNamespace

from app.config import Settings
from app.provider import chat_request_options, is_deepseek, record_model_request, record_model_usage


def test_deepseek_request_options_make_thinking_explicit() -> None:
    settings = Settings(openai_api_key="test-key")

    options = chat_request_options(settings)

    assert is_deepseek(settings) is True
    assert options == {
        "model": "deepseek-v4-pro",
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
    }


def test_deepseek_non_thinking_request_keeps_temperature() -> None:
    settings = Settings(openai_api_key="test-key", openai_thinking="disabled")

    options = chat_request_options(settings, temperature=0)

    assert options["extra_body"] == {"thinking": {"type": "disabled"}}
    assert options["temperature"] == 0
    assert "reasoning_effort" not in options


def test_generic_provider_does_not_receive_deepseek_options() -> None:
    settings = Settings(
        openai_api_key="test-key",
        openai_base_url="https://provider.example/v1",
        openai_model="provider-model",
    )

    options = chat_request_options(settings, temperature=0.2)

    assert is_deepseek(settings) is False
    assert options == {"model": "provider-model", "temperature": 0.2}


def test_model_metrics_track_real_requests_retries_and_usage() -> None:
    metrics: dict = {}

    record_model_request(metrics, "report")
    record_model_request(metrics, "report", retry=True)
    record_model_usage(
        metrics,
        SimpleNamespace(usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30, total_tokens=150)),
        "report",
    )

    assert metrics == {
        "model_calls": 2,
        "report_model_calls": 2,
        "model_retries": 1,
        "input_tokens": 120,
        "report_input_tokens": 120,
        "output_tokens": 30,
        "report_output_tokens": 30,
        "total_tokens": 150,
        "report_total_tokens": 150,
    }
