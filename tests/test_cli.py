import sys

import pytest
from rich.console import Console

import app.cli as cli
from app.models import AnalysisReport, CodeReference


def test_print_report_renders_complete_analysis(monkeypatch) -> None:
    console = Console(record=True, width=120)
    monkeypatch.setattr(cli, "console", console)
    report = AnalysisReport(
        summary="Parser crashes on empty input",
        root_cause="tokens[0] is read without a guard",
        confidence="high",
        evidence=[CodeReference(path="src/parser.py", lines="L2", reason="Direct list access")],
        proposed_changes=["Guard empty tokens"],
        patch="--- a/src/parser.py\n+++ b/src/parser.py\n",
        tests=["Exercise empty input"],
        risks=["May change error semantics"],
        files_examined=["src/parser.py"],
    )

    cli.print_report(report)

    rendered = console.export_text()
    assert "Parser crashes on empty input" in rendered
    assert "src/parser.py" in rendered
    assert "Guard empty tokens" in rendered
    assert "Exercise empty input" in rendered
    assert "May change error semantics" in rendered


def test_main_dispatches_analyze_command(monkeypatch) -> None:
    called: dict[str, str | None] = {}

    async def fake_analyze(url: str, save_patch: str | None = None) -> None:
        called.update(url=url, save_patch=save_patch)

    monkeypatch.setattr(cli, "cmd_analyze", fake_analyze)
    monkeypatch.setattr(
        sys,
        "argv",
        ["issue-agent", "analyze", "https://github.com/acme/widget/issues/1", "--save-patch", "fix.patch"],
    )

    cli.main()

    assert called == {"url": "https://github.com/acme/widget/issues/1", "save_patch": "fix.patch"}


def test_main_dispatches_chat_command(monkeypatch) -> None:
    called: dict[str, str | None] = {}

    async def fake_chat(url: str, save_patch: str | None = None) -> None:
        called.update(url=url, save_patch=save_patch)

    monkeypatch.setattr(cli, "cmd_chat", fake_chat)
    monkeypatch.setattr(sys, "argv", ["issue-agent", "chat", "https://github.com/acme/widget/issues/2"])

    cli.main()

    assert called == {"url": "https://github.com/acme/widget/issues/2", "save_patch": None}


# ── cmd_analyze / cmd_chat 主流程（补齐 48% → 90%+ 的低覆盖） ──


def _report_payload() -> dict:
    report = AnalysisReport(
        summary="Parser crashes on empty input",
        root_cause="tokens[0] read without guard",
        confidence="high",
        evidence=[CodeReference(path="src/parser.py", lines="L2", reason="Direct list access")],
        proposed_changes=["Guard empty tokens"],
        patch="--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-tokens[0]\n+if tokens: tokens[0]",
        tests=["Exercise empty input"],
        risks=["May change error semantics"],
    )
    return report.model_dump()


class _FakeEventsAgent:
    """mock IssueAgent：按预设事件流驱动 cmd_analyze / cmd_chat。"""

    def __init__(self, events, chat_reply: str = "chat reply") -> None:
        self._events = events
        self.chat_reply = chat_reply

    async def investigate_stream(self, url, *, session=None):
        for event in self._events:
            # 与真实 IssueAgent 一致：report 事件同步到 session（cmd_chat 的 /save 依赖）
            if session is not None and event.type == "report" and event.data:
                session.report = AnalysisReport(**event.data)
            yield event

    async def aclose(self) -> None:
        pass

    async def chat(self, session, message: str):
        from app.models import ChatResponse

        return ChatResponse(session_id=session.session_id, reply=self.chat_reply, tools_used=["read_file"])


def _standard_events():
    from app.events import done_event, report_event, start_event, tool_call_event, tool_result_event

    return [
        start_event("Parser issue", 3),
        tool_call_event("read_file", {"path": "src/parser.py"}, 1),
        tool_result_event("read_file", "def parse():\n    return None"),
        report_event(_report_payload()),
        done_event(),
    ]


def test_cmd_analyze_renders_report(monkeypatch, tmp_path, capsys) -> None:
    import asyncio

    agent = _FakeEventsAgent(_standard_events())
    monkeypatch.setattr(cli, "IssueAgent", lambda settings: agent)
    save_patch = tmp_path / "fix.patch"

    asyncio.run(cli.cmd_analyze("https://github.com/acme/widget/issues/1", save_patch=str(save_patch)))

    out = capsys.readouterr().out
    assert "Parser crashes on empty input" in out
    assert "Root Cause" in out
    assert "Guard empty tokens" in out
    assert "Patch:" in out
    # --save-patch 落盘
    assert save_patch.exists()
    assert "if tokens: tokens[0]" in save_patch.read_text(encoding="utf-8")


def test_cmd_analyze_error_exits(monkeypatch, capsys) -> None:
    import asyncio

    class _FailingAgent:
        async def investigate_stream(self, url, *, session=None):
            raise ValueError("bad url")
            yield  # pragma: no cover

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(cli, "IssueAgent", lambda settings: _FailingAgent())
    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(cli.cmd_analyze("https://github.com/acme/widget/issues/1"))
    assert exc_info.value.code == 1
    assert "bad url" in capsys.readouterr().out


def test_cmd_analyze_skips_save_when_no_patch(monkeypatch, tmp_path, capsys) -> None:
    import asyncio

    events = _standard_events()
    # 报告不带 patch
    payload = _report_payload()
    payload["patch"] = None
    from app.events import report_event

    events[3] = report_event(payload)
    agent = _FakeEventsAgent(events)
    monkeypatch.setattr(cli, "IssueAgent", lambda settings: agent)
    save_patch = tmp_path / "nopatch.patch"

    asyncio.run(cli.cmd_analyze("https://github.com/acme/widget/issues/1", save_patch=str(save_patch)))

    assert not save_patch.exists()
    assert "Parser crashes on empty input" in capsys.readouterr().out


def test_cmd_chat_investigates_then_chats(monkeypatch, tmp_path, capsys) -> None:
    import asyncio
    import builtins

    agent = _FakeEventsAgent(_standard_events(), chat_reply="chat reply text")
    monkeypatch.setattr(cli, "IssueAgent", lambda settings: agent)
    # 交互循环：第一次输入问题得到回复，第二次 /quit 退出
    inputs = iter(["再解释一下", "/quit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(inputs))

    asyncio.run(cli.cmd_chat("https://github.com/acme/widget/issues/1"))

    out = capsys.readouterr().out
    assert "Parser crashes on empty input" in out
    assert "chat reply text" in out
    assert "read_file" in out  # tools_used 展示


def test_cmd_chat_save_patch_command(monkeypatch, tmp_path, capsys) -> None:
    import asyncio
    import builtins

    agent = _FakeEventsAgent(_standard_events())
    monkeypatch.setattr(cli, "IssueAgent", lambda settings: agent)
    save_patch = tmp_path / "from-chat.patch"
    inputs = iter([f"/save {save_patch}", "/quit"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(inputs))

    asyncio.run(cli.cmd_chat("https://github.com/acme/widget/issues/1"))

    assert save_patch.exists()
    assert "if tokens: tokens[0]" in save_patch.read_text(encoding="utf-8")
    assert "Patch saved" in capsys.readouterr().out


def test_cmd_chat_quit_on_eof(monkeypatch, capsys) -> None:
    import asyncio
    import builtins

    agent = _FakeEventsAgent(_standard_events())
    monkeypatch.setattr(cli, "IssueAgent", lambda settings: agent)
    # Ctrl+D / 管道结束：EOFError 应安静退出
    monkeypatch.setattr(builtins, "input", lambda prompt="": (_ for _ in ()).throw(EOFError()))

    asyncio.run(cli.cmd_chat("https://github.com/acme/widget/issues/1"))  # 不应抛异常

    assert "Interactive Mode" in capsys.readouterr().out
