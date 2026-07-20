import json
import os
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("SESSION_DB_PATH", ":memory:")

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
    def __init__(
        self,
        content: str | None = None,
        tool_calls: list | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _FakeResponse:
    def __init__(self, message: _FakeMessage) -> None:
        self.choices = [SimpleNamespace(message=message, finish_reason="stop")]


class _FakeStreamChunk:
    """单个 SSE chunk：模拟 OpenAI stream=True 模式下的 delta。"""

    def __init__(self, content: str | None = None, reasoning_content: str | None = None) -> None:
        delta = SimpleNamespace(
            content=content,
            reasoning_content=reasoning_content,
            reasoning=None,
        )
        self.choices = [SimpleNamespace(delta=delta, finish_reason="stop")]


class _FakeStream:
    """async iterator over _FakeStreamChunk。供 stream=True 调用使用。"""

    def __init__(self, chunks: list) -> None:
        self._chunks = list(chunks)
        self._index = 0

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> object:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


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
        if kwargs.get("stream"):
            # stream=True：把 _FakeResponse 拆成单个 delta chunk；
            # 如果调用方直接传入 chunk 列表（list），按原样包装成 _FakeStream。
            if isinstance(response, list):
                return _FakeStream(response)
            message = response.choices[0].message
            chunk = _FakeStreamChunk(
                content=message.content,
                reasoning_content=getattr(message, "reasoning_content", None),
            )
            return _FakeStream([chunk])
        return response


class _FakeClient:
    def __init__(self, responses: list) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(responses))

    async def close(self) -> None:
        pass


@pytest.fixture
def settings() -> Settings:
    return Settings(openai_api_key="test-key", max_agent_iterations=5, independent_review=False)


@pytest.fixture
def make_settings() -> Settings:
    return Settings(openai_api_key="test-key", independent_review=False)


@pytest.fixture
def make_agent():
    def _factory(*, settings_kwargs: dict | None = None, **kwargs: object) -> IssueAgent:
        base_kwargs = {
            "openai_api_key": "test-key",
            "max_agent_iterations": 5,
            "independent_review": False,
        }
        if settings_kwargs:
            base_kwargs.update(settings_kwargs)
        return IssueAgent(
            Settings(**base_kwargs),
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
    def _factory(
        content: str | None = None,
        tool_calls: list | None = None,
        reasoning_content: str | None = None,
    ) -> _FakeResponse:
        return _FakeResponse(_FakeMessage(content=content, tool_calls=tool_calls, reasoning_content=reasoning_content))

    return _factory


@pytest.fixture
def fake_client():
    def _factory(responses: list) -> _FakeClient:
        return _FakeClient(responses)

    return _factory
