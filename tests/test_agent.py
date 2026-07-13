import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent import IssueAgent, ModelResponseError, _serialize_message, _trim_session_messages
from app.models import ChatResponse, SourceFile
from app.sessions import Session
from app.tools import ToolExecutor


from app.models import IssueData as _IssueData

class _MockGitHub:
    def __init__(self, *args, **kwargs):
        self.get_file = AsyncMock(side_effect=self._get_file)
        self.get_file_history = AsyncMock(return_value=[])
        self.list_branches = AsyncMock(return_value=[])
        self.get_file_at_commit = AsyncMock()
        self.get_issue = AsyncMock(return_value=_IssueData(
            owner="acme", repo="widget", number=1, title="Test Bug", body="test",
            labels=["bug"], comments=[], default_branch="main",
        ))
        self.get_tree = AsyncMock(return_value=["src/parser.py", "README.md"])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def _get_file(self, issue, path):
        return SourceFile(path=path, content="def parse():\n    return None\n")


@pytest.mark.parametrize(
    ("lines", "line_count", "expected"),
    [
        (None, 3, True), ("L1", 3, True), ("L2-L3", 3, True), ("L2-3", 3, True),
        ("2-3", 3, False), ("L0", 3, False), ("L3-L2", 3, False), ("L1-L4", 3, False),
    ],
)
def test_has_valid_lines(lines: str | None, line_count: int, expected: bool) -> None:
    assert IssueAgent._has_valid_lines(lines, line_count) is expected


def test_serialize_message_plain_content() -> None:
    msg = SimpleNamespace(content="hello", tool_calls=None)
    assert _serialize_message(msg) == {"role": "assistant", "content": "hello"}


def test_serialize_message_none_content() -> None:
    msg = SimpleNamespace(content=None, tool_calls=None)
    assert _serialize_message(msg) == {"role": "assistant", "content": ""}


def test_serialize_message_with_tool_calls() -> None:
    tc = SimpleNamespace(id="call_1", type="function", function=SimpleNamespace(name="read_file", arguments='{"path": "src/a.py"}'))
    msg = SimpleNamespace(content="thinking", tool_calls=[tc])
    result = _serialize_message(msg)
    assert result["role"] == "assistant"
    assert result["content"] == "thinking"
    assert result["tool_calls"][0]["id"] == "call_1"
    assert result["tool_calls"][0]["function"]["name"] == "read_file"


async def test_investigate_stream_yields_events(make_agent, fake_client, fake_response, fake_tool_call, monkeypatch, make_issue) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    tc = fake_tool_call("read_file", {"path": "src/parser.py"})
    report_json = json.dumps({
        "summary": "Parser bug", "root_cause": "Fails at src/parser.py L1", "confidence": "high",
        "evidence": [{"path": "src/parser.py", "lines": "L1", "reason": "parse call"}],
        "proposed_changes": ["Fix"], "patch": None, "tests": [], "risks": [],
    })
    responses = [
        fake_response(tool_calls=[tc]),
        fake_response(content="Done"),
        fake_response(content=report_json),
    ]
    agent = make_agent(client=fake_client(responses))

    events = []
    async for event in agent.investigate_stream("https://github.com/acme/widget/issues/1"):
        events.append(event)

    types = [e.type for e in events]
    assert "start" in types
    assert "tool_call" in types
    assert "tool_result" in types
    assert "report" in types
    assert "done" in types


async def test_investigate_returns_report(make_agent, fake_client, fake_response, fake_tool_call, monkeypatch, make_issue) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    tc = fake_tool_call("read_file", {"path": "src/parser.py"})
    report_json = json.dumps({
        "summary": "Parser bug", "root_cause": "Fails at src/parser.py L1", "confidence": "high",
        "evidence": [{"path": "src/parser.py", "lines": "L1", "reason": "parse call"}],
        "proposed_changes": ["Fix"], "patch": None, "tests": [], "risks": [],
    })
    responses = [
        fake_response(tool_calls=[tc]),
        fake_response(content="Done"),
        fake_response(content=report_json),
    ]
    agent = make_agent(client=fake_client(responses))
    report = await agent.investigate("https://github.com/acme/widget/issues/1")
    assert report.confidence == "high"
    assert report.summary == "Parser bug"


async def test_generate_report_keeps_valid_evidence(make_agent, fake_client, fake_response, make_issue) -> None:
    report_json = json.dumps({
        "summary": "Parser bug", "root_cause": "Parser fails at src/parser.py L1", "confidence": "high",
        "evidence": [{"path": "src/parser.py", "lines": "L1", "reason": "parse call"}],
        "proposed_changes": ["Fix parser"], "patch": None, "tests": ["Add regression test"], "risks": [],
    })
    agent = make_agent(client=fake_client([fake_response(content=report_json)]))
    from app.config import Settings
    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), make_issue(), ["src/parser.py"])
    executor._file_cache["src/parser.py"] = "parse(value)"
    executor.files_read.append("src/parser.py")
    report = await agent._generate_report([{"role": "user", "content": "investigate"}], executor)
    assert report.confidence == "high"
    assert len(report.evidence) == 1
    assert report.evidence[0].path == "src/parser.py"
    assert report.evidence_audit.valid_references == 1


async def test_generate_report_filters_unread_and_invalid_evidence(make_agent, fake_client, fake_response, make_issue) -> None:
    report_json = json.dumps({
        "summary": "Bug", "root_cause": "Parser fails", "confidence": "high",
        "evidence": [{"path": "src/parser.py", "lines": "L99", "reason": "out of range"}, {"path": "src/unread.py", "lines": "L1", "reason": "never read"}],
        "proposed_changes": ["Fix it"], "patch": None, "tests": [], "risks": [],
    })
    agent = make_agent(client=fake_client([fake_response(content=report_json)]))
    from app.config import Settings
    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), make_issue(), ["src/parser.py"])
    executor._file_cache["src/parser.py"] = "parse(value)"
    executor.files_read.append("src/parser.py")
    report = await agent._generate_report([{"role": "user", "content": "investigate"}], executor)
    assert report.evidence == []
    assert report.confidence == "low"
    assert "尚未验证" in report.risks[0] or "not been verified" in report.risks[0]


async def test_generate_report_raises_on_invalid_confidence(make_agent, fake_client, fake_response, make_issue) -> None:
    report_json = json.dumps({"summary": "Bug", "root_cause": "Fails", "confidence": "very-high", "evidence": [], "proposed_changes": [], "patch": None, "tests": [], "risks": []})
    agent = make_agent(client=fake_client([fake_response(content=report_json)]))
    from app.config import Settings
    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), make_issue(), [])
    with pytest.raises(ModelResponseError):
        await agent._generate_report([{"role": "user", "content": "investigate"}], executor)


async def test_generate_report_raises_on_empty_response(make_agent, fake_client, fake_response, make_issue) -> None:
    agent = make_agent(client=fake_client([fake_response(content=None)]))
    from app.config import Settings
    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), make_issue(), [])
    with pytest.raises(ModelResponseError):
        await agent._generate_report([{"role": "user", "content": "investigate"}], executor)


async def test_chat_returns_reply_without_tools(make_agent, fake_client, fake_response, monkeypatch, make_issue) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    agent = make_agent(client=fake_client([fake_response(content="根因是 parser 缺少异常处理")]))
    session = Session(session_id="s1", issue_url="https://github.com/a/b/issues/1")
    session.issue = make_issue()
    session.tree = ["src/parser.py"]
    result = await agent.chat(session, "根因是什么？")
    assert isinstance(result, ChatResponse)
    assert result.session_id == "s1"
    assert result.reply == "根因是 parser 缺少异常处理"
    assert result.tools_used == []


async def test_chat_uses_tools_and_returns_reply(make_agent, fake_client, fake_response, fake_tool_call, monkeypatch, make_issue) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    tc = fake_tool_call("read_file", {"path": "src/parser.py"})
    responses = [fake_response(tool_calls=[tc]), fake_response(content="parser 代码在第 1 行")]
    agent = make_agent(client=fake_client(responses))
    session = Session(session_id="s2", issue_url="https://github.com/a/b/issues/1")
    session.issue = make_issue()
    session.tree = ["src/parser.py"]
    result = await agent.chat(session, "看一下 parser 代码")
    assert "read_file" in result.tools_used
    assert "src/parser.py" in session.files_read
    assert result.reply == "parser 代码在第 1 行"


async def test_chat_returns_depth_limit_message(make_agent, fake_client, fake_response, fake_tool_call, monkeypatch, make_issue) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    tc = fake_tool_call("list_directory", {"path": ""})
    responses = [fake_response(tool_calls=[tc]) for _ in range(6)]
    agent = make_agent(client=fake_client(responses))
    session = Session(session_id="s3", issue_url="https://github.com/a/b/issues/1")
    session.issue = make_issue()
    session.tree = ["src/a.py"]
    result = await agent.chat(session, "列出文件")
    assert "深度上限" in result.reply or "depth" in result.reply.lower()


async def test_chat_raises_on_uninitialized_session(make_agent) -> None:
    agent = make_agent()
    session = Session(session_id="s4", issue_url="https://github.com/a/b/issues/1")
    with pytest.raises(ValueError, match="Session not initialized"):
        await agent.chat(session, "hello")


async def test_investigate_populates_session(make_agent, fake_client, fake_response, fake_tool_call, monkeypatch, make_issue) -> None:
    issue = make_issue()

    class _MockGitHubWithIssue(_MockGitHub):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.get_issue = AsyncMock(return_value=issue)
            self.get_tree = AsyncMock(return_value=["src/parser.py", "README.md"])

    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHubWithIssue)
    tc = fake_tool_call("read_file", {"path": "src/parser.py"})
    report_json = json.dumps({
        "summary": "Parser bug", "root_cause": "Parser fails at src/parser.py L1", "confidence": "high",
        "evidence": [{"path": "src/parser.py", "lines": "L1", "reason": "parse call"}],
        "proposed_changes": ["Fix parser"], "patch": "--- a/src/parser.py\n+++ b/src/parser.py\n", "tests": ["Add test"], "risks": [],
    })
    responses = [fake_response(tool_calls=[tc]), fake_response(content="Done"), fake_response(content=report_json)]
    agent = make_agent(client=fake_client(responses))
    session = Session(session_id="inv1", issue_url="https://github.com/acme/widget/issues/1")
    report = await agent.investigate("https://github.com/acme/widget/issues/1", session=session)
    assert session.issue is issue
    assert session.tree == ["src/parser.py", "README.md"]
    assert "src/parser.py" in session.files_read
    assert session.report is not None
    assert session.report.confidence == report.confidence


async def test_injected_client_is_reused_and_not_closed(make_agent) -> None:
    closed = False

    class _FakeClient:
        async def close(self):
            nonlocal closed
            closed = True

    fake = _FakeClient()
    agent = make_agent(client=fake)
    assert agent._client is fake
    await agent.aclose()
    assert closed is False
    assert agent._client is None


def test_build_initial_messages_includes_issue_and_tree(make_agent, make_issue) -> None:
    agent = make_agent()
    issue = make_issue(title="Crash on empty input", body="steps to reproduce")
    tree = ["src/main.py", "src/parser.py", "docs/readme.md"]
    messages = agent._build_initial_messages(issue, tree)
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "Crash on empty input" in messages[1]["content"]
    assert "src/parser.py" in messages[1]["content"]


def test_trim_session_messages_keeps_latest_complete_turn() -> None:
    messages = [
        {"role": "user", "content": "old question"}, {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new question"}, {"role": "assistant", "content": "new answer"},
    ]
    _trim_session_messages(messages, max_chars=22)
    assert messages == [{"role": "user", "content": "new question"}, {"role": "assistant", "content": "new answer"}]


def test_trim_session_messages_compacts_oversized_tool_turn() -> None:
    messages = [
        {"role": "user", "content": "inspect"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": "x" * 100}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "source" * 20},
        {"role": "assistant", "content": "answer"},
    ]
    _trim_session_messages(messages, max_chars=20)
    assert messages == [{"role": "user", "content": "inspect"}, {"role": "assistant", "content": "answer"}]


async def test_chat_serializes_requests_for_same_session(make_agent, monkeypatch) -> None:
    agent = make_agent()
    session = Session(session_id="locked", issue_url="https://github.com/a/b/issues/1")
    active = 0
    maximum_active = 0

    async def fake_chat(current_session, message):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ChatResponse(session_id=current_session.session_id, reply=message)

    monkeypatch.setattr(agent, "_chat", fake_chat)
    first, second = await asyncio.gather(agent.chat(session, "first"), agent.chat(session, "second"))
    assert [first.reply, second.reply] == ["first", "second"]
    assert maximum_active == 1
