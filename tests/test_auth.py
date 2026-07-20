from fastapi.testclient import TestClient

import app.auth as auth_module
from app.config import Settings
from app.main import app


def test_api_key_authentication_allows_public_routes(monkeypatch) -> None:
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: Settings(openai_api_key="test-key", api_key="local-secret"),
    )

    response = TestClient(app).get("/health")

    assert response.status_code == 200


def test_api_key_authentication_rejects_missing_and_invalid_keys(monkeypatch) -> None:
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: Settings(openai_api_key="test-key", api_key="local-secret"),
    )
    client = TestClient(app)

    missing = client.get("/sessions")
    invalid = client.get("/sessions", headers={"X-API-Key": "wrong"})

    assert missing.status_code == 401
    assert invalid.status_code == 403


def test_api_key_authentication_accepts_constant_time_match(monkeypatch) -> None:
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: Settings(openai_api_key="test-key", api_key="local-secret"),
    )

    with TestClient(app) as client:
        response = client.get("/sessions", headers={"X-API-Key": "local-secret"})

    assert response.status_code == 200
