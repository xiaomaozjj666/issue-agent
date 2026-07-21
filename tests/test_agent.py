import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent import IssueAgent, ModelResponseError, _serialize_message, _trim_session_messages
from app.config import Settings
from app.models import ChatResponse, SourceFile
from app.models import IssueData as _IssueData
from app.sessions import Session
from app.tools import ToolExecutor


class _MockGitHub:
    def __init__(self, *args, **kwargs):
        self.get_file = AsyncMock(side_effect=self._get_file)
        self.get_file_history = AsyncMock(return_value=[])
        self.list_branches = AsyncMock(return_value=[])
        self.get_file_at_commit = AsyncMock()
        self.get_issue = AsyncMock(
            return_value=_IssueData(
                owner="acme",
                repo="widget",
                number=1,
                title="Test Bug",
                body="test",
                labels=["bug"],
                comments=[],
                default_branch="main",
            )
        )
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
        (None, 3, True),
        ("L1", 3, True),
        ("L2-L3", 3, True),
        ("L2-3", 3, True),
        ("2-3", 3, False),
        ("L0", 3, False),
        ("L3-L2", 3, False),
        ("L1-L4", 3, False),
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
    tc = SimpleNamespace(
        id="call_1", type="function", function=SimpleNamespace(name="read_file", arguments='{"path": "src/a.py"}')
    )
    msg = SimpleNamespace(content="thinking", tool_calls=[tc])
    result = _serialize_message(msg)
    assert result["role"] == "assistant"
    assert result["content"] == "thinking"
    assert result["tool_calls"][0]["id"] == "call_1"
    assert result["tool_calls"][0]["function"]["name"] == "read_file"


def test_serialize_message_preserves_reasoning_content_for_deepseek_tool_turns() -> None:
    tc = SimpleNamespace(
        id="call_1", type="function", function=SimpleNamespace(name="read_file", arguments='{"path":"a.py"}')
    )
    msg = SimpleNamespace(content="", reasoning_content="I should inspect a.py", tool_calls=[tc])

    result = _serialize_message(msg)

    assert result["reasoning_content"] == "I should inspect a.py"


async def test_investigate_stream_yields_events(
    make_agent, fake_client, fake_response, fake_tool_call, monkeypatch, make_issue
) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    tc = fake_tool_call("read_file", {"path": "src/parser.py"})
    report_json = json.dumps(
        {
            "summary": "Parser bug",
            "root_cause": "Fails at src/parser.py L1",
            "confidence": "high",
            "evidence": [{"path": "src/parser.py", "lines": "L1", "reason": "parse call"}],
            "proposed_changes": ["Fix"],
            "patch": None,
            "tests": [],
            "risks": [],
        }
    )
    responses = [
        fake_response(tool_calls=[tc], reasoning_content="The parser source is relevant."),
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
    calls = agent._client.chat.completions.calls
    assert calls[0]["model"] == "deepseek-v4-pro"
    assert calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert calls[0]["reasoning_effort"] == "high"
    assert "tool_choice" not in calls[0]
    assistant_tool_turn = next(message for message in calls[1]["messages"] if message.get("tool_calls"))
    assert assistant_tool_turn["reasoning_content"] == "The parser source is relevant."


async def test_investigation_runs_independent_reviewer(fake_client, fake_response, fake_tool_call, monkeypatch) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    tc = fake_tool_call("read_file", {"path": "src/parser.py"})
    report_data = {
        "summary": "Parser bug",
        "root_cause": "Fails at src/parser.py L1",
        "confidence": "medium",
        "evidence": [{"path": "src/parser.py", "lines": "L1", "reason": "parse definition"}],
        "proposed_changes": ["Fix"],
        "patch": None,
        "tests": ["Add regression test"],
        "risks": [],
    }
    review_data = {
        "verdict": "approved",
        "summary": "The evidence supports the report.",
        "findings": ["The regression test covers the failure path."],
        "report": report_data,
    }
    client = fake_client(
        [
            fake_response(tool_calls=[tc]),
            fake_response(content="Done"),
            fake_response(content=json.dumps(report_data)),
            fake_response(content=json.dumps(review_data)),
        ]
    )
    agent = IssueAgent(
        Settings(openai_api_key="test-key", max_agent_iterations=5, independent_review=True),
        client=client,
    )
    session = Session(session_id="reviewed", issue_url="https://github.com/acme/widget/issues/1")

    events = [
        event
        async for event in agent.investigate_stream(
            "https://github.com/acme/widget/issues/1",
            session=session,
        )
    ]

    assert "review" in [event.type for event in events]
    assert session.report is not None
    assert session.report.review_audit.status == "approved"
    assert session.metrics["review_calls"] == 1
    assert session.metrics["model_calls"] == 4


async def test_investigation_degrades_safely_when_reviewer_fails(fake_client, fake_response, monkeypatch) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    report_data = {
        "summary": "Parser bug",
        "root_cause": "Unverified parser failure",
        "confidence": "low",
        "evidence": [],
        "proposed_changes": ["Investigate further"],
        "patch": None,
        "tests": [],
        "risks": [],
    }
    client = fake_client(
        [
            fake_response(content="Done"),
            fake_response(content=json.dumps(report_data)),
            fake_response(content="{}"),
        ]
    )
    agent = IssueAgent(
        Settings(openai_api_key="test-key", max_agent_iterations=5, independent_review=True),
        client=client,
    )

    report = await agent.investigate("https://github.com/acme/widget/issues/1")

    assert report.summary == "Parser bug"
    assert report.review_audit.status == "unavailable"
    assert report.review_audit.summary in report.risks


async def test_investigate_returns_report(
    make_agent, fake_client, fake_response, fake_tool_call, monkeypatch, make_issue
) -> None:
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    tc = fake_tool_call("read_file", {"path": "src/parser.py"})
    report_json = json.dumps(
        {
            "summary": "Parser bug",
            "root_cause": "Fails at src/parser.py L1",
            "confidence": "high",
            "evidence": [{"path": "src/parser.py", "lines": "L1", "reason": "parse call"}],
            "proposed_changes": ["Fix"],
            "patch": None,
            "tests": [],
            "risks": [],
        }
    )
    responses = [
        fake_response(tool_calls=[tc]),
        fake_response(content="Done"),
        fake_response(content=report_json),
    ]
    agent = make_agent(client=fake_client(responses))
    report = await agent.investigate("https://github.com/acme/widget/issues/1")
    assert report.confidence == "medium"
    assert report.summary == "Parser bug"


async def test_generate_report_keeps_valid_evidence(make_agent, fake_client, fake_response, make_issue) -> None:
    report_json = json.dumps(
        {
            "summary": "Parser bug",
            "root_cause": "Parser fails at src/parser.py L1",
            "confidence": "high",
            "evidence": [{"path": "src/parser.py", "lines": "L1", "reason": "parse call"}],
            "proposed_changes": ["Fix parser"],
            "patch": None,
            "tests": ["Add regression test"],
            "risks": [],
        }
    )
    agent = make_agent(client=fake_client([fake_response(content=report_json)]))
    from app.config import Settings

    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), make_issue(), ["src/parser.py"])
    executor._file_cache["src/parser.py"] = "parse(value)"
    executor.files_read.append("src/parser.py")
    report = await agent._generate_report([{"role": "user", "content": "investigate"}], executor)
    assert report.confidence == "medium"
    assert len(report.evidence) == 1
    assert report.evidence[0].path == "src/parser.py"
    assert report.evidence_audit.valid_references == 1


async def test_generate_report_filters_unread_and_invalid_evidence(
    make_agent, fake_client, fake_response, make_issue
) -> None:
    report_json = json.dumps(
        {
            "summary": "Bug",
            "root_cause": "Parser fails",
            "confidence": "high",
            "evidence": [
                {"path": "src/parser.py", "lines": "L99", "reason": "out of range"},
                {"path": "src/unread.py", "lines": "L1", "reason": "never read"},
            ],
            "proposed_changes": ["Fix it"],
            "patch": None,
            "tests": [],
            "risks": [],
        }
    )
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
    report_json = json.dumps(
        {
            "summary": "Bug",
            "root_cause": "Fails",
            "confidence": "very-high",
            "evidence": [],
            "proposed_changes": [],
            "patch": None,
            "tests": [],
            "risks": [],
        }
    )
    # max_report_retries=2 时需要两个相同的无效响应让两次尝试都失败
    agent = make_agent(
        client=fake_client([fake_response(content=report_json), fake_response(content=report_json)]),
        settings_kwargs={"max_report_retries": 2},
    )
    from app.config import Settings

    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), make_issue(), [])
    with pytest.raises(ModelResponseError):
        await agent._generate_report([{"role": "user", "content": "investigate"}], executor)


async def test_generate_report_retries_on_invalid_json_then_succeeds(
    make_agent, fake_client, fake_response, make_issue
) -> None:
    # 模拟 DeepSeek thinking 模式偶发问题：第一次返回工具调用参数 JSON 而非 AnalysisReport
    invalid_json = json.dumps(
        {
            "action": "read_file",
            "path": "src/parser.py",
            "start_line": 1,
            "end_line": 10,
        }
    )
    valid_json = json.dumps(
        {
            "summary": "Parser bug",
            "root_cause": "Fails at src/parser.py L1",
            "confidence": "high",
            "evidence": [{"path": "src/parser.py", "lines": "L1", "reason": "parse call"}],
            "proposed_changes": ["Fix"],
            "patch": None,
            "tests": [],
            "risks": [],
        }
    )
    agent = make_agent(
        client=fake_client([fake_response(content=invalid_json), fake_response(content=valid_json)]),
        settings_kwargs={"max_report_retries": 2},
    )
    from app.config import Settings

    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), make_issue(), ["src/parser.py"])
    executor._file_cache["src/parser.py"] = "parse(value)"
    executor.files_read.append("src/parser.py")
    report = await agent._generate_report([{"role": "user", "content": "investigate"}], executor)
    # 第一次响应被拒，重试后成功
    assert report.summary == "Parser bug"
    assert len(agent._client.chat.completions.calls) == 2
    # 重试时的 messages 应包含错误反馈
    retry_call_messages = agent._client.chat.completions.calls[1]["messages"]
    assert any("AnalysisReport schema" in str(m.get("content", "")) for m in retry_call_messages)


async def test_generate_report_retries_three_times_with_thinking_fallback(
    make_agent, fake_client, fake_response, make_issue
) -> None:
    # 默认 max_report_retries=3：前两次 thinking enabled 失败，第三次降级 disabled 后成功
    invalid_json = json.dumps({"action": "read_file", "path": "x", "start_line": 1, "end_line": 2})
    valid_json = json.dumps(
        {
            "summary": "Fixed",
            "root_cause": "Root",
            "confidence": "low",
            "evidence": [],
            "proposed_changes": [],
            "patch": None,
            "tests": [],
            "risks": [],
        }
    )
    agent = make_agent(
        client=fake_client(
            [
                fake_response(content=invalid_json),
                fake_response(content=invalid_json),
                fake_response(content=valid_json),
            ]
        )
    )
    from app.config import Settings

    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), make_issue(), [])
    report = await agent._generate_report([{"role": "user", "content": "investigate"}], executor)
    assert report.summary == "Fixed"
    assert len(agent._client.chat.completions.calls) == 3
    # 前两次 thinking enabled，最后一次降级 disabled
    assert agent._client.chat.completions.calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert agent._client.chat.completions.calls[1]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert agent._client.chat.completions.calls[2]["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_generate_report_raises_on_empty_response(make_agent, fake_client, fake_response, make_issue) -> None:
    # max_report_retries=2 时需要两个空响应让两次尝试都失败
    agent = make_agent(
        client=fake_client([fake_response(content=None), fake_response(content=None)]),
        settings_kwargs={"max_report_retries": 2},
    )
    from app.config import Settings

    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), make_issue(), [])
    with pytest.raises(ModelResponseError):
        await agent._generate_report([{"role": "user", "content": "investigate"}], executor)


def test_build_report_messages_skips_tool_history_and_keeps_system_issue_and_files(
    make_issue,
) -> None:
    from app.config import Settings

    agent = IssueAgent(Settings(openai_api_key="test-key", max_total_context_chars=80_000))
    issue = make_issue(title="Crash", body="steps")
    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), issue, ["src/parser.py"])
    executor._file_cache["src/parser.py"] = "def parse():\n    return None\n"
    executor.files_read.append("src/parser.py")

    messages = [
        {"role": "system", "content": "SYSTEM_PROMPT"},
        {"role": "user", "content": "ISSUE_CONTEXT"},
        {"role": "assistant", "content": "thinking", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "TOOL_RESULT"},
        {"role": "assistant", "content": "more thinking", "tool_calls": [{"id": "c2"}]},
        {"role": "tool", "tool_call_id": "c2", "content": "TOOL_RESULT_2"},
    ]

    final_messages = agent._build_report_messages(messages, executor)

    assert len(final_messages) == 2
    assert final_messages[0] == {"role": "system", "content": "SYSTEM_PROMPT"}
    user_content = final_messages[1]["content"]
    assert "ISSUE_CONTEXT" in user_content
    assert "TOOL_RESULT" not in user_content
    assert "TOOL_RESULT_2" not in user_content
    assert "src/parser.py" in user_content
    assert "L1: def parse():" in user_content
    assert "L2:     return None" in user_content
    assert "Source files examined (with line numbers):" in user_content


def test_build_report_messages_truncates_files_when_budget_exhausted(make_issue) -> None:
    from app.config import Settings

    # 用最小允许的 max_total_context_chars 和巨大的文件内容，触发预算耗尽分支
    agent = IssueAgent(Settings(openai_api_key="test-key", max_total_context_chars=5_000))
    issue = make_issue()
    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), issue, ["src/a.py", "src/b.py"])
    executor._file_cache["src/a.py"] = "a" * 8_000
    executor._file_cache["src/b.py"] = "b" * 8_000
    executor.files_read.extend(["src/a.py", "src/b.py"])

    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "ISSUE"},
    ]
    final_messages = agent._build_report_messages(messages, executor)

    assert len(final_messages) == 2
    user_content = final_messages[1]["content"]
    # 即使文件被截断或跳过，最终输出指令仍应附加在末尾
    assert "Based on your investigation" in user_content or "基于以上调查" in user_content


def test_build_report_messages_handles_missing_system_prompt(make_issue) -> None:
    from app.config import Settings

    agent = IssueAgent(Settings(openai_api_key="test-key"))
    issue = make_issue()
    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), issue, [])

    messages = [{"role": "user", "content": "investigate"}]
    final_messages = agent._build_report_messages(messages, executor)

    # 无 system prompt 时只产出一条 user 消息
    assert len(final_messages) == 1
    assert final_messages[0]["role"] == "user"
    assert "investigate" in final_messages[0]["content"]


def test_build_report_messages_handles_no_files_read(make_issue) -> None:
    from app.config import Settings

    agent = IssueAgent(Settings(openai_api_key="test-key"))
    issue = make_issue()
    executor = ToolExecutor(MagicMock(), Settings(openai_api_key="test-key"), issue, [])

    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "ISSUE"},
    ]
    final_messages = agent._build_report_messages(messages, executor)

    assert len(final_messages) == 2
    assert final_messages[0] == {"role": "system", "content": "SYS"}
    assert "ISSUE" in final_messages[1]["content"]
    # 即使没有已读文件，也应附加 final output prompt
    user_content = final_messages[1]["content"]
    assert "Based on your investigation" in user_content or "基于以上调查" in user_content


def test_build_report_messages_keeps_bounded_tool_findings(make_issue) -> None:
    settings = Settings(openai_api_key="test-key", max_total_context_chars=20_000)
    agent = IssueAgent(settings)
    issue = make_issue()
    executor = ToolExecutor(MagicMock(), settings, issue, ["src/parser.py"])
    executor._record_observation(
        "get_file_history",
        {"path": "src/parser.py"},
        "History for src/parser.py: abc1234 fix parser regression",
    )

    final_messages = agent._build_report_messages(
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "ISSUE"}],
        executor,
    )

    user_content = final_messages[-1]["content"]
    assert "Investigation ledger" in user_content
    assert "abc1234 fix parser regression" in user_content


async def test_chat_returns_reply_without_tools(
    make_agent, fake_client, fake_response, monkeypatch, make_issue
) -> None:
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


async def test_chat_uses_tools_and_returns_reply(
    make_agent, fake_client, fake_response, fake_tool_call, monkeypatch, make_issue
) -> None:
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


async def test_chat_returns_depth_limit_message(
    make_agent, fake_client, fake_response, fake_tool_call, monkeypatch, make_issue
) -> None:
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


async def test_chat_stream_yields_delta_and_done(make_agent, fake_client, monkeypatch, make_issue) -> None:
    """chat_stream 应逐 chunk 透传 delta，最终发出 done 事件携带完整 reply。"""
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    # 模拟多 chunk 流式输出：每个 chunk 一段字符
    from tests.conftest import _FakeStreamChunk

    chunks = [
        _FakeStreamChunk(content="根因是 "),
        _FakeStreamChunk(content="parser "),
        _FakeStreamChunk(content="缺少异常处理"),
    ]
    agent = make_agent(client=fake_client([chunks]))
    session = Session(session_id="stream1", issue_url="https://github.com/a/b/issues/1")
    session.issue = make_issue()
    session.tree = ["src/parser.py"]

    events = []
    async for event in agent.chat_stream(session, "根因是什么？"):
        events.append(event)

    deltas = [e for e in events if e["type"] == "delta"]
    dones = [e for e in events if e["type"] == "done"]
    assert len(deltas) == 3
    assert deltas[0]["content"] == "根因是 "
    assert deltas[1]["content"] == "parser "
    assert deltas[2]["content"] == "缺少异常处理"
    assert len(dones) == 1
    assert dones[0]["reply"] == "根因是 parser 缺少异常处理"
    assert dones[0]["tools_used"] == []
    assert dones[0]["type"] == "done"
    # 验证 user 消息已入 session.messages
    assert session.messages[0]["role"] == "user"
    assert session.messages[0]["content"] == "根因是什么？"
    # 验证 assistant 回复已入 session.messages
    assert session.messages[-1]["role"] == "assistant"
    assert session.messages[-1]["content"] == "根因是 parser 缺少异常处理"


async def test_chat_stream_with_tool_calls(make_agent, fake_client, monkeypatch, make_issue) -> None:
    """chat_stream 在工具调用轮次后应继续流式输出最终回复。"""
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    from tests.conftest import _FakeStreamChunk

    # 第 1 轮：tool_call delta（分两个 chunk 模拟 id 和 arguments 分批抵达）
    tool_call_delta_1 = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="read_file", arguments=""),
    )
    tool_call_delta_2 = SimpleNamespace(
        index=0,
        id=None,
        function=SimpleNamespace(name=None, arguments='{"path": "src/parser.py"}'),
    )
    tool_chunks = [
        _FakeStreamChunk(tool_calls=[tool_call_delta_1]),
        _FakeStreamChunk(tool_calls=[tool_call_delta_2]),
    ]
    # 第 2 轮：纯文本回复
    reply_chunks = [
        _FakeStreamChunk(content="parser "),
        _FakeStreamChunk(content="代码在第 1 行"),
    ]
    agent = make_agent(client=fake_client([tool_chunks, reply_chunks]))
    session = Session(session_id="stream2", issue_url="https://github.com/a/b/issues/1")
    session.issue = make_issue()
    session.tree = ["src/parser.py"]

    events = []
    async for event in agent.chat_stream(session, "看一下 parser 代码"):
        events.append(event)

    tool_calls_events = [e for e in events if e["type"] == "tool_call"]
    deltas = [e for e in events if e["type"] == "delta"]
    dones = [e for e in events if e["type"] == "done"]

    assert len(tool_calls_events) == 1
    assert tool_calls_events[0]["name"] == "read_file"
    assert len(deltas) == 2
    assert dones[0]["reply"] == "parser 代码在第 1 行"
    assert "read_file" in dones[0]["tools_used"]
    assert "src/parser.py" in session.files_read


async def test_chat_stream_uninitialized_session_yields_error(make_agent) -> None:
    """未初始化的 session 应通过 error 事件透传，而非抛异常中断流。"""
    agent = make_agent()
    session = Session(session_id="stream3", issue_url="https://github.com/a/b/issues/1")
    events = []
    async for event in agent.chat_stream(session, "hello"):
        events.append(event)

    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert "Session not initialized" in errors[0]["message"]


async def test_chat_stream_depth_limit_reached(make_agent, fake_client, monkeypatch, make_issue) -> None:
    """达到迭代上限时应发出 done 事件携带 depth_limit 文案。"""
    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHub)
    from tests.conftest import _FakeStreamChunk

    # 每轮都触发 list_directory 工具调用，max_agent_iterations=5 后触发 depth_limit
    tool_call_delta = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="list_directory", arguments='{"path": ""}'),
    )
    tool_chunks = [_FakeStreamChunk(tool_calls=[tool_call_delta])]
    # 5 轮 tool_calls
    responses = [tool_chunks for _ in range(5)]
    agent = make_agent(client=fake_client(responses))
    session = Session(session_id="stream4", issue_url="https://github.com/a/b/issues/1")
    session.issue = make_issue()
    session.tree = ["src/a.py"]

    events = []
    async for event in agent.chat_stream(session, "列出文件"):
        events.append(event)

    dones = [e for e in events if e["type"] == "done"]
    assert len(dones) == 1
    assert "深度上限" in dones[0]["reply"] or "depth" in dones[0]["reply"].lower()


async def test_investigate_populates_session(
    make_agent, fake_client, fake_response, fake_tool_call, monkeypatch, make_issue
) -> None:
    issue = make_issue()

    class _MockGitHubWithIssue(_MockGitHub):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.get_issue = AsyncMock(return_value=issue)
            self.get_tree = AsyncMock(return_value=["src/parser.py", "README.md"])

    monkeypatch.setattr("app.agent.GitHubClient", _MockGitHubWithIssue)
    tc = fake_tool_call("read_file", {"path": "src/parser.py"})
    report_json = json.dumps(
        {
            "summary": "Parser bug",
            "root_cause": "Parser fails at src/parser.py L1",
            "confidence": "high",
            "evidence": [{"path": "src/parser.py", "lines": "L1", "reason": "parse call"}],
            "proposed_changes": ["Fix parser"],
            "patch": "--- a/src/parser.py\n+++ b/src/parser.py\n",
            "tests": ["Add test"],
            "risks": [],
        }
    )
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
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]
    _trim_session_messages(messages, max_chars=22)
    assert messages == [{"role": "user", "content": "new question"}, {"role": "assistant", "content": "new answer"}]


def test_trim_session_messages_compacts_oversized_tool_turn() -> None:
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": "x" * 100}}],
        },
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
