from app.config import Settings
from app.provider import chat_request_options, is_deepseek


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
