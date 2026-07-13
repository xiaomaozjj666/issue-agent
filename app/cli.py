import argparse
import asyncio
import sys
from pathlib import Path

from app.agent import IssueAgent, ModelResponseError
from app.config import get_settings
from app.github import GitHubError, parse_issue_url
from app.models import AnalysisReport
from app.sessions import SessionManager


def print_report(report: AnalysisReport) -> None:
    print()
    print("=" * 60)
    print(f"  摘要：{report.summary}")
    print("=" * 60)
    print()
    print(f"根因：{report.root_cause}")
    print()
    print(f"置信度：{report.confidence}")
    print(f"有效证据：{report.evidence_audit.valid_references} 条")
    print(f"根因有据：{'是' if report.evidence_audit.root_cause_supported else '否'}")
    print(f"检查文件：{len(report.files_examined)} 个")

    if report.evidence:
        print()
        print("代码证据：")
        for ev in report.evidence:
            print(f"  - {ev.path} {ev.lines or ''}: {ev.reason or ''}")

    if report.proposed_changes:
        print()
        print("修复建议：")
        for i, change in enumerate(report.proposed_changes, 1):
            print(f"  {i}. {change}")

    if report.patch:
        print()
        print("补丁 (unified diff)：")
        print(report.patch)

    if report.tests:
        print()
        print("建议测试：")
        for i, test in enumerate(report.tests, 1):
            print(f"  {i}. {test}")

    if report.risks:
        print()
        print("风险提示：")
        for risk in report.risks:
            print(f"  - {risk}")

    if report.files_examined:
        print()
        print(f"检查的文件：{', '.join(report.files_examined)}")
    print()


async def cmd_analyze(url: str, save_patch: str | None = None) -> None:
    agent = IssueAgent(get_settings())
    try:
        print(f"正在分析：{url}")
        print("请等待（30-90 秒）...")
        report = await agent.investigate(url)
        print_report(report)
        if save_patch and report.patch:
            Path(save_patch).write_text(report.patch, encoding="utf-8")
            print(f"补丁已保存到：{save_patch}")
            print()
    except (ValueError, GitHubError, ModelResponseError) as error:
        print(f"错误：{error}", file=sys.stderr)
        sys.exit(1)
    finally:
        await agent.aclose()


async def cmd_chat(url: str, save_patch: str | None = None) -> None:
    agent = IssueAgent(get_settings())
    session_manager = SessionManager()

    try:
        parse_issue_url(url)
        session = session_manager.create(url)

        print(f"正在分析：{url}")
        print("请等待（30-90 秒）...")
        report = await agent.investigate(url, session=session)
        print_report(report)

        if save_patch and report.patch:
            Path(save_patch).write_text(report.patch, encoding="utf-8")
            print(f"补丁已保存到：{save_patch}")
            print()

        print("=" * 60)
        print("  进入交互模式，输入问题继续对话")
        print("  /save <file>  保存补丁")
        print("  /quit         退出")
        print("=" * 60)
        print()

        while True:
            try:
                user_input = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_input:
                continue
            if user_input in ("/quit", "/exit"):
                break
            if user_input.startswith("/save"):
                parts = user_input.split(maxsplit=1)
                if len(parts) < 2:
                    print("用法：/save <文件路径>")
                    continue
                if not session.report or not session.report.patch:
                    print("当前没有可保存的补丁")
                    continue
                Path(parts[1]).write_text(session.report.patch, encoding="utf-8")
                print(f"补丁已保存到：{parts[1]}")
                continue

            print()
            try:
                response = await agent.chat(session, user_input)
                print(response.reply)
                if response.tools_used:
                    print()
                    print(f"[工具调用：{', '.join(response.tools_used)}]")
            except (GitHubError, ModelResponseError) as error:
                print(f"错误：{error}")
            print()
    except ValueError as error:
        print(f"错误：{error}", file=sys.stderr)
        sys.exit(1)
    finally:
        await agent.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GitHub Issue Agent — 基于工具调用的自主代码调查",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="一次性分析 issue")
    p_analyze.add_argument("url", help="GitHub issue URL")
    p_analyze.add_argument("--save-patch", help="保存补丁到指定文件")

    p_chat = sub.add_parser("chat", help="交互式对话分析")
    p_chat.add_argument("url", help="GitHub issue URL")
    p_chat.add_argument("--save-patch", help="保存补丁到指定文件")

    args = parser.parse_args()

    if args.command == "analyze":
        asyncio.run(cmd_analyze(args.url, args.save_patch))
    elif args.command == "chat":
        asyncio.run(cmd_chat(args.url, args.save_patch))


if __name__ == "__main__":
    main()
