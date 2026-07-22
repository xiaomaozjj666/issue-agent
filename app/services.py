"""Service layer: session state, formatting, and PR application logic.

Extracted from main.py so the HTTP layer only handles request/response assembly.
All cross-module state updates, report formatting, PR apply/rollback, and event
recording live here as stateless functions for testability and reuse.

Performance note: ``record_agent_event`` only appends events to the event log
without persisting the full session on every SSE event.  Session state is
persisted at key phase transitions (start, report, done) to reduce SQLite
write amplification from 30-50 writes per investigation to ~5.
"""

import logging
from time import monotonic

from fastapi import HTTPException

from app.config import Settings
from app.events import AgentEvent
from app.github import GitHubClient, GitHubError
from app.models import AnalysisReport, ApplyFixRequest, CreatePRResponse, SessionSummary
from app.sessions import Session, SessionConflictError, SessionManager
from app.tools import validate_pr_proposal

logger = logging.getLogger(__name__)


def format_report_text(report: AnalysisReport) -> str:
    """把结构化报告渲染成纯文本，给 chat 模式和 CLI 使用。"""
    lines = [
        f"Summary: {report.summary}",
        "",
        f"Root Cause: {report.root_cause}",
        "",
        f"Confidence: {report.confidence}",
    ]
    if report.evidence:
        lines.append("")
        lines.append("Code Evidence:")
        for ev in report.evidence:
            lines.append(f"  - {ev.path} {ev.lines or ''}: {ev.reason or ''}")
    if report.proposed_changes:
        lines.append("")
        lines.append("Proposed Changes:")
        for i, change in enumerate(report.proposed_changes, 1):
            lines.append(f"  {i}. {change}")
    if report.patch:
        lines.append("")
        lines.append("Patch:")
        lines.append(report.patch)
    if report.tests:
        lines.append("")
        lines.append("Suggested Tests:")
        for i, test in enumerate(report.tests, 1):
            lines.append(f"  {i}. {test}")
    if report.risks:
        lines.append("")
        lines.append("Risks:")
        for risk in report.risks:
            lines.append(f"  - {risk}")
    if report.review_audit.status != "not_run":
        lines.append("")
        lines.append(f"Independent Review: {report.review_audit.status}")
        if report.review_audit.summary:
            lines.append(report.review_audit.summary)
        for finding in report.review_audit.findings:
            lines.append(f"  - {finding}")
    return "\n".join(lines)


def session_summary(session: Session) -> SessionSummary:
    """从 Session 构造对外暴露的 SessionSummary（不含 messages/report/events）。"""
    owner = session.issue.owner if session.issue else ""
    repo = session.issue.repo if session.issue else ""
    issue_number = session.issue.number if session.issue else None
    head_sha = session.issue.head_sha if session.issue else ""
    fallback_title = session.issue.title if session.issue else session.issue_url
    return SessionSummary(
        session_id=session.session_id,
        issue_url=session.issue_url,
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        head_sha=head_sha,
        title=session.display_title or fallback_title,
        status=session.status,
        phase=session.phase,
        error_message=session.error_message,
        archived=session.archived_at is not None,
        version=session.version,
        metrics=session.metrics,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def event_payload(event: AgentEvent) -> dict:
    """把 AgentEvent 转成持久化用的 dict（type/data/message 三段）。"""
    return {"type": event.type, "data": event.data, "message": event.message}


async def record_agent_event(
    manager: SessionManager,
    session: Session,
    event: AgentEvent,
    started_at: float,
) -> None:
    """Append an agent event to the durable event log.

    Only persists the event record; session state (phase/metrics) is updated
    in-memory but NOT written to the database on every call.  This reduces
    SQLite write amplification significantly during streaming.  The caller
    is responsible for calling ``manager.save(session)`` at key checkpoints
    (e.g. phase transitions, completion).
    """
    await manager.append_event(session.session_id, event_payload(event))
    if event.type == "phase" and event.data:
        session.phase = str(event.data.get("phase", session.phase))
    session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)


async def mark_session_failed(manager: SessionManager, session: Session | None, error: Exception) -> None:
    """把异常标记到 session 状态。冲突时记录警告但不抛出，避免覆盖原始异常。"""
    if session is None:
        return
    session.status = "failed"
    session.phase = "failed"
    session.error_message = str(error)[:500]
    try:
        await manager.save(session)
    except SessionConflictError:
        logger.warning("Could not mark concurrently updated session %s as failed", session.session_id)


async def finish_cancelled_session(
    manager: SessionManager,
    session_id: str,
    event: AgentEvent,
    started_at: float,
) -> None:
    """取消流程的终态写入：session 标记为 cancelled + 事件入库。"""
    try:
        session = await manager.get(session_id)
        if session is None:
            return
        session.status = "cancelled"
        session.phase = "cancelled"
        session.error_message = None
        session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)
        await manager.append_event(session_id, event_payload(event))
        await manager.save(session)
    except SessionConflictError:
        # 并发更新冲突不应阻塞取消流程；session 状态由冲突方负责推进
        logger.warning("SessionConflictError while finalizing cancelled session %s", session_id)


async def mark_stream_interrupted(manager: SessionManager, session_id: str, started_at: float) -> None:
    """客户端断开时把 session 标记为 interrupted。

    关键：内部必须捕获 SessionConflictError，不能让它替换外层的 CancelledError，
    否则会破坏 asyncio 任务取消传播链，导致任务无法正确清理。
    """
    try:
        session = await manager.get(session_id)
        if session is None or session.status != "running":
            return
        session.status = "failed"
        session.phase = "interrupted"
        session.error_message = "Connection closed before the investigation completed"
        session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)
        await manager.append_event(
            session_id,
            {"type": "interrupted", "data": None, "message": session.error_message},
        )
        await manager.save(session)
    except SessionConflictError:
        logger.warning("SessionConflictError while marking stream interrupted for session %s", session_id)


async def apply_fix(
    session_id: str,
    request: ApplyFixRequest,
    *,
    settings: Settings,
    session_mgr: SessionManager,
) -> CreatePRResponse:
    """应用 PR 提案：校验 → 建分支 → 改文件 → 开 PR，失败时回滚分支。

    从 main.py 抽出后签名显式接收 settings 和 session_mgr，
    避免 HTTP 层函数内部直接读全局配置和单例，便于测试和未来替换实现。
    """
    if not settings.write_mode:
        raise HTTPException(status_code=403, detail="Write mode is disabled")

    session = await session_mgr.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    proposal = await session_mgr.get_pr_proposal(session_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="No pending PR proposal for this session")

    if not request.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to create the PR")

    if session.issue is None:
        raise HTTPException(status_code=409, detail="Session investigation is incomplete")

    try:
        proposal = validate_pr_proposal(
            settings,
            branch=proposal["branch"],
            title=proposal["title"],
            body=proposal["body"],
            changes=proposal.get("changes", []),
            default_branch=session.issue.default_branch,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=f"Stored PR proposal is invalid: {error}") from error

    branch_created = False
    try:
        async with GitHubClient(settings.github_token, write_enabled=True) as github:
            branch = proposal["branch"]
            base = session.issue.default_branch
            base_sha = await github.get_branch_sha(session.issue.owner, session.issue.repo, base)

            await github.create_branch(session.issue.owner, session.issue.repo, branch, base_sha)
            branch_created = True

            for change in proposal.get("changes", []):
                await github.create_or_update_file(
                    session.issue.owner,
                    session.issue.repo,
                    change["path"],
                    change["content"],
                    branch,
                    change.get("message", "fix: apply patch"),
                )

            pr = await github.create_pull_request(
                session.issue.owner,
                session.issue.repo,
                branch,
                base,
                proposal["title"],
                proposal["body"],
            )
            await session_mgr.delete_pr_proposal(session_id)
            return CreatePRResponse(pr_url=pr["pr_url"], branch=branch)
    except GitHubError as error:
        if branch_created:
            try:
                async with GitHubClient(settings.github_token, write_enabled=True) as rollback_github:
                    await rollback_github.delete_branch(
                        session.issue.owner,
                        session.issue.repo,
                        proposal["branch"],
                    )
            except GitHubError:
                logger.exception("Failed to roll back branch %s", proposal["branch"])
        logger.exception("Failed to apply fix for session %s", session_id)
        raise HTTPException(status_code=502, detail=str(error)) from error
