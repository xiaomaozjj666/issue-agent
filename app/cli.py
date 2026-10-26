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
from app.models import AnalysisReport, PatchValidationRequest
from app.patch_validation import PatchValidationError, validate_patch
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

    if report.patch_validation is not None:
        validation = report.patch_validation
        console.print()
        console.print(f"[bold]Patch Validation:[/] {validation.status}")
        console.print(validation.summary)
        for execution in [validation.before_patch, validation.after_patch, *validation.regressions]:
            console.print(
                f"  - {execution.phase}: {execution.status} (exit={execution.exit_code}, {execution.duration_ms}ms)"
            )
        console.print(f"  Base SHA: {validation.base_sha}")
        console.print(f"  Workspace cleaned: {validation.workspace_cleaned}")

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


async def cmd_validate_patch(
    repository_path: str,
    patch_file: str,
    test_command: str,
    *,
    regression_commands: list[str],
    base_sha: str | None,
    timeout_seconds: float,
    max_output_bytes: int,
    confirm: bool,
) -> int:
    """Validate a local patch and emit machine-readable evidence as JSON."""
    try:
        patch = Path(patch_file).read_text(encoding="utf-8")
        request = PatchValidationRequest(
            repository_path=repository_path,
            patch=patch,
            test_command=test_command,
            regression_commands=regression_commands,
            base_sha=base_sha,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            confirm=confirm,
        )
        evidence = await validate_patch(request)
    except (OSError, ValueError, PermissionError, PatchValidationError) as error:
        console.print(f"[red]Error: {error}[/]")
        return 2
    console.print_json(evidence.model_dump_json())
    return 0 if evidence.status == "passed" else 1


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="GitHub Issue Agent — code investigation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="One-shot issue analysis")
    p_analyze.add_argument("url", help="GitHub issue URL")
    p_analyze.add_argument("--save-patch", help="Save patch to file")

    p_chat = sub.add_parser("chat", help="Interactive chat analysis")
    p_chat.add_argument("url", help="GitHub issue URL")
    p_chat.add_argument("--save-patch", help="Save patch to file")

    p_validate = sub.add_parser(
        "validate-patch",
        help="Run trusted red-green tests against a local patch in isolated Git worktrees",
    )
    p_validate.add_argument("repository_path", help="Local Git repository (its working tree is never modified)")
    p_validate.add_argument("--patch-file", required=True, help="UTF-8 unified diff to validate")
    p_validate.add_argument("--test-command", required=True, help="Trusted red-green test command")
    p_validate.add_argument(
        "--regression-command",
        action="append",
        default=[],
        help="Trusted regression command run after patching; repeatable",
    )
    p_validate.add_argument("--base-sha", help="Fixed base commit (7-40 hex); defaults to current HEAD")
    p_validate.add_argument("--timeout-seconds", type=float, default=120.0)
    p_validate.add_argument("--max-output-bytes", type=int, default=12_000)
    p_validate.add_argument("--confirm", action="store_true", help="Explicitly approve local command execution")

    args = parser.parse_args()
    if args.command == "analyze":
        asyncio.run(cmd_analyze(args.url, args.save_patch))
    elif args.command == "chat":
        asyncio.run(cmd_chat(args.url, args.save_patch))
    elif args.command == "validate-patch":
        raise SystemExit(
            asyncio.run(
                cmd_validate_patch(
                    args.repository_path,
                    args.patch_file,
                    args.test_command,
                    regression_commands=args.regression_command,
                    base_sha=args.base_sha,
                    timeout_seconds=args.timeout_seconds,
                    max_output_bytes=args.max_output_bytes,
                    confirm=args.confirm,
                )
            )
        )


if __name__ == "__main__":
    main()
