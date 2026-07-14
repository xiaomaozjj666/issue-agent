import json
import os
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest

from app.agent import IssueAgent
from app.config import Settings
from app.models import IssueData


class _FakeToolCall:
    def __init__(self, name: str, arguments: dict, call_id: str = "call_1") -> None:
        self.id = call_id
        self.type = "function"
        self.function = SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments) if isinstance(arguments, dict) else arguments,
        )


class _FakeMessage:
    def __init__(self, content: str | None = None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeResponse:
    def __init__(self, message: _FakeMessage) -> None:
        self.choices = [SimpleNamespace(message=message, finish_reason="stop")]


class _FakeCompletions:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self._index = 0
        self.calls: list[dict] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._index >= len(self._responses):
            raise RuntimeError("No more mock responses")
        response = self._responses[self._index]
        self._index += 1
        return response


class _FakeClient:
    def __init__(self, responses: list) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(responses))

    async def close(self) -> None:
        pass


@pytest.fixture
def settings() -> Settings:
    return Settings(openai_api_key="test-key", max_agent_iterations=5)


@pytest.fixture
def make_settings() -> Settings:
    return Settings(openai_api_key="test-key")


@pytest.fixture
def make_agent():
    def _factory(**kwargs: object) -> IssueAgent:
        return IssueAgent(
            Settings(openai_api_key="test-key", max_agent_iterations=5),
            **kwargs,
        )

    return _factory


@pytest.fixture
def make_issue():
    def _factory(**changes: object) -> IssueData:
        values: dict[str, object] = {
            "owner": "acme",
            "repo": "widget",
            "number": 1,
            "title": "Parser failure",
            "body": "A" * 4_000,
            "labels": ["bug"],
            "comments": ["B" * 2_000],
            "default_branch": "main",
        }
        values.update(changes)
        return IssueData.model_validate(values)

    return _factory


@pytest.fixture
def fake_tool_call():
    def _factory(name: str, arguments: dict, call_id: str = "call_1") -> _FakeToolCall:
        return _FakeToolCall(name, arguments, call_id)

    return _factory


@pytest.fixture
def fake_response():
    def _factory(content: str | None = None, tool_calls: list | None = None) -> _FakeResponse:
        return _FakeResponse(_FakeMessage(content=content, tool_calls=tool_calls))

    return _factory


@pytest.fixture
def fake_client():
    def _factory(responses: list) -> _FakeClient:
        return _FakeClient(responses)

    return _factory
