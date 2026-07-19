import sys

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
