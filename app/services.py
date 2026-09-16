"""Service layer: session state, formatting, and PR application logic.

Extracted from main.py so the HTTP layer only handles request/response assembly.
All cross-module state updates, report formatting, PR apply/rollback, and event
recording live here as stateless functions for testability and reuse.

Performance note: ``record_agent_event`` only appends events to the event log
without persisting the full session on every SSE event.  Session state is
persisted at key phase transitions (start, report, done) to reduce SQLite
write amplification from 30-50 writes per investigation to ~5.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from time import monotonic

from fastapi import HTTPException

from app.config import Settings
from app.events import AgentEvent
from app.github import GitHubClient, GitHubError, GitHubPRCreatedError
from app.models import AnalysisReport, ApplyFixRequest, CreatePRResponse, SessionSummary
from app.sessions import Session, SessionConflictError, SessionManager
from app.tools import validate_pr_proposal

logger = logging.getLogger(__name__)

# 并发闸门打满 / 同一会话重复发起时的用户可见消息（error 事件正文，前端原样展示）。
BUSY_INVESTIGATIONS = (
    "Too many investigations are already running. Please retry in a moment."
)
SESSION_ALREADY_RUNNING = (
    "This session already has a running investigation. Wait for it to finish, or cancel it first."
)

# 活跃心跳间隔：必须显著小于 session_stale_after_seconds（默认 300s），
# 否则后台 stale recovery 会把正在跑的调查误判成孤儿并抢走终态（返回 409）。
_TOUCH_INTERVAL_SECONDS = 15.0


class InvestigationGate:
    """调查并发闸门：计数 + 微锁，``try_acquire`` 绝不排队等待。

    早期实现用 ``asyncio.wait_for(sem.acquire(), timeout=0.05)`` 模拟非阻塞，但在高负载
    机器上（CI 并行跑多个矩阵任务）50ms 可能不足以完成一次 acquire，导致**合法请求被
    误判为「闸门已满」**并返回 429。这里改用计数器 + 一把只在极短临界区内持有的锁：
    判定与占位是原子的，且不依赖任何定时器/事件循环时序。
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._in_use = 0
        self._lock = asyncio.Lock()

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def limit(self) -> int:
        return self._limit

    async def try_acquire(self) -> bool:
        """Reserve a slot, or return False immediately when the gate is full."""
        async with self._lock:
            if self._in_use >= self._limit:
                return False
            self._in_use += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            if self._in_use > 0:
                self._in_use -= 1


async def acquire_investigation_slot(gate: InvestigationGate) -> bool:
    """Reserve one investigation slot; ``False`` means "gate is full, try later"."""
    return await gate.try_acquire()


@asynccontextmanager
async def periodic_touch(manager: SessionManager, session_id: str | None) -> AsyncIterator[None]:
    """Refresh ``updated_at`` every ``_TOUCH_INTERVAL_SECONDS`` while work is running.

    ``/stream`` touches the session on every SSE keepalive, but blocking investigations
    (``/chat``) have no heartbeat at all: a long run would be flipped to
    ``failed/interrupted`` by stale recovery, and the final save then fails the
    optimistic lock, handing the user a 409 even though the work succeeded.
    """
    if session_id is None:
        yield
        return
    stop = asyncio.Event()

    async def beat() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_TOUCH_INTERVAL_SECONDS)
                return
            except TimeoutError:
                pass
            try:
                await manager.touch(session_id)
            except Exception:  # noqa: BLE001 — 心跳失败不应中断调查
                logger.debug("periodic touch failed for session %s", session_id, exc_info=True)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


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


# Transient event types that should NOT be persisted to the event log.
# These fire at per-token frequency during streaming (thinking/reasoning deltas)
# and would generate thousands of individual SQLite INSERT+commit operations,
# severely throttling the LLM stream. They are surfaced via SSE to the frontend
# but have no replay value after the session ends.
_TRANSIENT_EVENT_TYPES: frozenset[str] = frozenset({"thinking", "reasoning"})


async def record_agent_event(
    manager: SessionManager,
    session: Session,
    event: AgentEvent,
    started_at: float,
) -> None:
    """Append an agent event to the durable event log.

    Transient events (thinking/reasoning deltas) are skipped — they fire at
    per-token frequency and provide no replay value.  The caller is responsible
    for calling ``manager.save(session)`` at key checkpoints (phase transitions,
    completion).
    """
    if event.type not in _TRANSIENT_EVENT_TYPES:
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


async def _persist_interrupted_detached(
    manager: SessionManager, session_id: str, started_at: float
) -> None:
    """脱离取消作用域的后台任务：确保 interrupted 终态一定落库。

    ``mark_stream_interrupted`` 在客户端断开（外层任务被取消）的上下文中调用，
    其内部的 ``await manager.save`` 可能立即再次被取消。把同样的终态写入调度为
    独立任务后，它不受当前任务的取消影响，从而保证 sessions.db 不会滞留 running。
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
    except Exception:
        logger.exception("Failed to persist interrupted state for session %s", session_id)


async def mark_stream_interrupted(manager: SessionManager, session_id: str, started_at: float) -> None:
    """客户端断开时把 session 标记为 interrupted。

    关键：在客户端断开（外层 asyncio 任务被取消）的上下文中，
    ``await manager.save`` 可能立即再次抛出 CancelledError，导致终态无法落库、
    sessions.db 滞留 running。因此：
    1) 正常路径直接 await 落库（保持同步语义，便于测试与正常的断连场景）；
    2) 一旦在落库途中捕获 CancelledError，把同样的终态写入调度为脱离取消作用域的
       后台任务（``asyncio.create_task``），确保 interrupted 终态一定落地，
       同时继续向外传播取消，不破坏 asyncio 任务取消链。
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
    except asyncio.CancelledError:
        # 落库途中被取消：把终态写入后台任务，确保它不随当前任务一起被取消。
        # 调度后立即重抛 CancelledError，维持外层取消传播链。
        asyncio.create_task(_persist_interrupted_detached(manager, session_id, started_at))
        raise
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

    if session.status == "running":
        raise HTTPException(status_code=409, detail="Session is still running — wait for it to complete")

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

    # 审计：写下「哪个提案（内容哈希）被应用到了哪个仓库/分支」，便于事后追责。
    proposal_digest = hashlib.sha256(
        json.dumps(proposal, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    logger.info(
        "Applying PR proposal for session %s: repo=%s/%s branch=%s files=%d digest=%s",
        session_id,
        session.issue.owner,
        session.issue.repo,
        proposal["branch"],
        len(proposal.get("changes", [])),
        proposal_digest,
    )

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
        # 关键边界：若 PR 已创建成功（仅响应校验失败），绝不能删除分支——
        # 删除 head 分支会让 GitHub 自动关闭刚创建的 PR。
        if branch_created and not isinstance(error, GitHubPRCreatedError):
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
