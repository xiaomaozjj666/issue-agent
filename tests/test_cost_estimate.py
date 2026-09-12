from types import SimpleNamespace

from app.config import Settings
from app.provider import apply_cost_estimate, estimate_usd_cost, record_model_usage


def test_estimate_usd_cost_from_tokens() -> None:
    cost = estimate_usd_cost(
        {"input_tokens": 1_000_000, "output_tokens": 500_000},
        input_price_per_million=0.27,
        output_price_per_million=1.10,
    )
    assert cost == round(0.27 + 0.55, 6)


def test_estimate_usd_cost_none_when_no_usage() -> None:
    assert estimate_usd_cost({}, input_price_per_million=0.27, output_price_per_million=1.10) is None
    assert estimate_usd_cost(None, input_price_per_million=0.27, output_price_per_million=1.10) is None


def test_apply_cost_estimate_writes_metric() -> None:
    settings = Settings(openai_api_key="test-key")
    metrics: dict = {}
    record_model_usage(
        metrics,
        SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000)),
        "exploration",
    )
    apply_cost_estimate(metrics, settings)
    assert metrics["estimated_cost_usd"] == round(settings.input_token_price_per_million, 6)


def test_apply_cost_estimate_skips_when_unknown() -> None:
    settings = Settings(openai_api_key="test-key")
    metrics: dict = {"model_calls": 1}
    apply_cost_estimate(metrics, settings)
    assert "estimated_cost_usd" not in metrics
