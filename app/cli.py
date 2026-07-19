"""GitHub Issue Agent CLI — rich-powered terminal interface."""

import argparse
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from rich.table import Table

from app.agent import IssueAgent, ModelResponseError
from app.config import get_settings
from app.github import GitHubError, parse_issue_url
from app.logging_config import setup_logging
from app.models import AnalysisReport
from app.sessions import SessionManager

console = Console()


def print_report(report: AnalysisReport) -> None:
    console.print()
    console.rule("[bold cyan]Analysis Report[/]")
    console.print(Panel(report.summary, title="Summary", border_style="blue"))
    console.print(f"[bold]Root Cause:[/] {report.root_cause}")
    console.print()

    badge_style = {"high": "red", "medium": "yellow", "low": "green"}
    style = badge_style.get(report.confidence, "white")
    console.print(f"[bold]Confidence:[/] [{style}]{report.confidence}[/]")
    console.print(f"Valid Evidence: {report.evidence_audit.valid_references}")
    console.print(f"Root Cause Supported: {'Yes' if report.evidence_audit.root_cause_supported else 'No'}")
    console.print(f"Files Examined: {len(report.files_examined)}")

    if report.evidence:
        console.print()
        table = Table(title="Code Evidence", border_style="dim")
        table.add_column("File", style="cyan")
        table.add_column("Lines", style="yellow")
        table.add_column("Reason")
        for ev in report.evidence:
            table.add_row(ev.path, ev.lines or "", ev.reason or "")
        console.print(table)

    if report.proposed_changes:
        console.print()
        console.print("[bold]Proposed Changes:[/]")
        for i, change in enumerate(report.proposed_changes, 1):
            console.print(f"  [green]{i}.[/] {change}")

    if report.patch:
        console.print()
        console.print("[bold]Patch:[/]")
        console.print(Syntax(report.patch, "diff", theme="monokai", line_numbers=True))

    if report.tests:
        console.print()
        console.print("[bold]Suggested Tests:[/]")
        for i, test in enumerate(report.tests, 1):
            console.print(f"  [green]{i}.[/] {test}")

    if report.risks:
        console.print()
        console.print("[bold red]Risks:[/]")
        for risk in report.risks:
            console.print(f"  - {risk}")

    if report.files_examined:
        console.print()
        console.print(f"[dim]Files examined: {', '.join(report.files_examined)}[/]")
    console.print()


async def cmd_analyze(url: str, save_patch: str | None = None) -> None:
    agent = IssueAgent(get_settings())
    try:
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            task = progress.add_task(f"[cyan]Analyzing {url}...", total=None)
            events = []
            async for event in agent.investigate_stream(url):
                events.append(event)
                data = event.data or {}
                if event.type == "start":
                    progress.update(
                        task, description=f"[cyan]Fetched issue, exploring {data.get('file_count', 0)} files..."
                    )
                elif event.type == "tool_call":
                    name = data.get("name", "")
                    args = data.get("args", {})
                    progress.update(task, description=f"[yellow]{name}({str(args)[:60]})")
                elif event.type == "report":
                    progress.update(task, description="[green]Analysis complete!")
                    report_data = event.data
                    if report_data:
                        report = AnalysisReport(**report_data)
                        print_report(report)
                        if save_patch and report.patch:
                            Path(save_patch).write_text(report.patch, encoding="utf-8")
                            console.print(f"[green]Patch saved to {save_patch}[/]")
    except (ValueError, GitHubError, ModelResponseError) as error:
        console.print(f"[red]Error: {error}[/]", style="red")
        sys.exit(1)
    finally:
        await agent.aclose()


async def cmd_chat(url: str, save_patch: str | None = None) -> None:
    agent = IssueAgent(get_settings())
    session_manager = SessionManager()
    try:
        parse_issue_url(url)
        session = await session_manager.create(url)

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
        ) as progress:
            task = progress.add_task(f"[cyan]Analyzing {url}...", total=None)
            async for event in agent.investigate_stream(url, session=session):
                data = event.data or {}
                if event.type == "tool_call":
                    progress.update(task, description=f"[yellow]Reading {data.get('name', '')}...")
                elif event.type == "report":
                    progress.update(task, description="[green]Analysis complete!")
                    if event.data:
                        report = AnalysisReport(**event.data)
                        print_report(report)
                        if save_patch and report.patch:
                            Path(save_patch).write_text(report.patch, encoding="utf-8")
                            console.print(f"[green]Patch saved to {save_patch}[/]")

        console.rule("[bold]Interactive Mode[/]")
        console.print("[dim]/save <file>  Save patch | /quit  Exit[/]")
        console.print()

        while True:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not user_input:
                continue
            if user_input in ("/quit", "/exit"):
                break
            if user_input.startswith("/save"):
                parts = user_input.split(maxsplit=1)
                if len(parts) < 2:
                    console.print("[yellow]Usage: /save <file_path>[/]")
                    continue
                if not session.report or not session.report.patch:
                    console.print("[yellow]No patch available to save[/]")
                    continue
                Path(parts[1]).write_text(session.report.patch, encoding="utf-8")
                console.print(f"[green]Patch saved to {parts[1]}[/]")
                continue
            console.print()
            try:
                response = await agent.chat(session, user_input)
                console.print(response.reply)
                if response.tools_used:
                    console.print(f"[dim]Tools: {', '.join(response.tools_used)}[/]")
            except (GitHubError, ModelResponseError) as error:
                console.print(f"[red]Error: {error}[/]")
            console.print()
    except ValueError as error:
        console.print(f"[red]Error: {error}[/]")
        sys.exit(1)
    finally:
        await agent.aclose()


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="GitHub Issue Agent — LLM-powered code investigation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="One-shot issue analysis")
    p_analyze.add_argument("url", help="GitHub issue URL")
    p_analyze.add_argument("--save-patch", help="Save patch to file")

    p_chat = sub.add_parser("chat", help="Interactive chat analysis")
    p_chat.add_argument("url", help="GitHub issue URL")
    p_chat.add_argument("--save-patch", help="Save patch to file")

    args = parser.parse_args()
    if args.command == "analyze":
        asyncio.run(cmd_analyze(args.url, args.save_patch))
    elif args.command == "chat":
        asyncio.run(cmd_chat(args.url, args.save_patch))


if __name__ == "__main__":
    main()
