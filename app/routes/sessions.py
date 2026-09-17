"""Session endpoints: list, detail, update, delete, cancel, report, PR proposal,
apply-fix (write mode), and full export/import."""

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.deps import SessionMgr
from app.models import (
    AnalysisReport,
    ApplyFixRequest,
    CreatePRResponse,
    IssueData,
    SessionDetail,
    SessionEventRecord,
    SessionSummary,
    SessionUpdateRequest,
)
from app.services import apply_fix, session_summary

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/sessions", response_model=list[SessionSummary])
async def list_sessions(
    manager: SessionMgr,
    archived: bool = False,
    q: str = Query(default="", max_length=160),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[SessionSummary]:
    sessions = await manager.list(
        archived=archived,
        query=q,
        limit=limit,
        offset=offset,
    )
    return [session_summary(session) for session in sessions]


@router.get("/session/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str, manager: SessionMgr) -> SessionDetail:
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionDetail(
        **session_summary(session).model_dump(),
        messages=session.messages,
        report=session.report,
        events=[SessionEventRecord.model_validate(event) for event in await manager.list_events(session_id)],
    )


@router.post("/session/{session_id}/cancel", response_model=SessionSummary)
async def cancel_session(session_id: str, manager: SessionMgr) -> SessionSummary:
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != "running":
        raise HTTPException(status_code=409, detail="Only a running investigation can be cancelled")
    if not await manager.request_cancel(session_id):
        raise HTTPException(status_code=409, detail="Investigation is no longer running")
    refreshed = await manager.get(session_id)
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session_summary(refreshed)


@router.patch("/session/{session_id}", response_model=SessionSummary)
async def update_session(session_id: str, request: SessionUpdateRequest, manager: SessionMgr) -> SessionSummary:
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    async with session.lock:
        if request.display_title is not None:
            session.display_title = request.display_title
        if request.archived is not None:
            session.archived_at = datetime.now(UTC).isoformat(timespec="seconds") if request.archived else None
        await manager.save(session)
    return session_summary(session)


@router.delete("/session/{session_id}", status_code=204)
async def delete_session(session_id: str, manager: SessionMgr) -> Response:
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    async with session.lock:
        await manager.delete(session_id)
    return Response(status_code=204)


@router.get("/session/{session_id}/report", response_model=AnalysisReport)
async def get_session_report(session_id: str, manager: SessionMgr) -> AnalysisReport:
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.report is None:
        raise HTTPException(status_code=404, detail="Report not yet generated for this session")
    return session.report


@router.post("/session/{session_id}/apply-fix", response_model=CreatePRResponse)
async def apply_fix_route(session_id: str, request: ApplyFixRequest, session_mgr: SessionMgr) -> CreatePRResponse:
    return await apply_fix(
        session_id,
        request,
        settings=get_settings(),
        session_mgr=session_mgr,
    )


@router.post("/apply-fix", response_model=CreatePRResponse, include_in_schema=False, deprecated=True)
async def apply_fix_legacy_route(
    request: ApplyFixRequest, session_id: str, session_mgr: SessionMgr
) -> CreatePRResponse:
    """Compatibility route for clients created before the session-scoped endpoint."""
    return await apply_fix(
        session_id,
        request,
        settings=get_settings(),
        session_mgr=session_mgr,
    )


@router.get("/session/{session_id}/proposal")
async def get_pr_proposal(session_id: str, manager: SessionMgr) -> dict:
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    proposal = await manager.get_pr_proposal(session_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="No pending PR proposal for this session")
    return {
        "branch": proposal["branch"],
        "title": proposal["title"],
        "body": proposal["body"],
        # 前端据此把「创建 PR」置灰并给出可行动原因（而不是等到点下去才 403）
        "write_mode": get_settings().write_mode,
        "changes": [
            {
                "path": change["path"],
                "message": change["message"],
                "proposed_lines": len(str(change.get("content", "")).splitlines()),
                "proposed_bytes": len(str(change.get("content", "")).encode("utf-8")),
                # 内容预览：人工确认必须看得到「到底要写什么」，而不是只有行数摘要。
                "preview": "\n".join(str(change.get("content", "")).splitlines()[:20]),
            }
            for change in proposal.get("changes", [])
        ],
    }


# ── E32 会话导出/导入 ──────────────────────────────────────
# 导出完整会话数据（含对话历史、工具调用事件、报告、指标），支持跨实例备份与迁移。
@router.get("/session/{session_id}/export")
async def export_session(session_id: str, manager: SessionMgr) -> JSONResponse:
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    events = await manager.list_events(session_id)
    payload = {
        "format": "issue-agent-session",
        "version": 1,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "session": {
            "session_id": session.session_id,
            "issue_url": session.issue_url,
            "issue": session.issue.model_dump() if session.issue else None,
            "tree": session.tree,
            "messages": session.messages,
            "files_read": session.files_read,
            "file_cache": session.file_cache,
            "report": session.report.model_dump() if session.report else None,
            "display_title": session.display_title,
            "status": session.status,
            "phase": session.phase,
            "metrics": session.metrics,
            "error_message": session.error_message,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        },
        "events": events,
        "pending_pr": session.pending_pr,
    }
    filename = f"session-{session_id}.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/session/import", response_model=SessionSummary)
async def import_session(request: Request, manager: SessionMgr) -> SessionSummary:
    """导入会话 JSON，创建新会话（生成新 session_id，保留原始数据）。"""
    settings = get_settings()
    # 两阶段大小校验：先看 Content-Length 早拒（避免读取超大 body 浪费带宽），
    # 再校验实际 body 字节数（防御 chunked transfer 不带 Content-Length 的场景）。
    content_length = request.headers.get("Content-Length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from None
        if declared > settings.max_session_import_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Import payload too large: {declared} bytes exceeds limit {settings.max_session_import_bytes}",
            )

    raw_body = await request.body()
    if len(raw_body) > settings.max_session_import_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Import payload too large: {len(raw_body)} bytes exceeds limit {settings.max_session_import_bytes}",
        )

    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    if not isinstance(body, dict) or body.get("format") != "issue-agent-session":
        raise HTTPException(status_code=400, detail="Not a valid issue-agent session export")
    sess_data = body.get("session")
    if not isinstance(sess_data, dict) or not sess_data.get("issue_url"):
        raise HTTPException(status_code=400, detail="Missing session.issue_url in import payload")

    # 创建新会话（生成新 session_id）
    new_session = await manager.create(sess_data["issue_url"])
    # 填充原始数据
    new_session.tree = sess_data.get("tree", []) or []
    new_session.messages = sess_data.get("messages", []) or []
    new_session.files_read = sess_data.get("files_read", []) or []
    raw_file_cache = sess_data.get("file_cache", {})
    new_session.file_cache = raw_file_cache if isinstance(raw_file_cache, dict) else {}
    new_session.display_title = sess_data.get("display_title")
    raw_metrics = sess_data.get("metrics", {})
    new_session.metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    new_session.error_message = sess_data.get("error_message")
    # 状态：导入的会话标记为 completed（避免被 stale 恢复逻辑误判为 running），
    # phase 与 status 保持一致，避免前端 UI 逻辑混乱
    new_session.status = "completed"
    new_session.phase = "completed"

    # 恢复 issue 对象
    issue_data = sess_data.get("issue")
    if isinstance(issue_data, dict):
        try:
            new_session.issue = IssueData.model_validate(issue_data)
        except Exception:
            logger.warning("Failed to validate issue data on import; storing as None", exc_info=True)
            new_session.issue = None

    # 恢复 report 对象
    report_data = sess_data.get("report")
    if isinstance(report_data, dict):
        try:
            new_session.report = AnalysisReport.model_validate(report_data)
        except Exception:
            logger.warning("Failed to validate report data on import; storing as None", exc_info=True)
            new_session.report = None

    async with new_session.lock:
        await manager.save(new_session)

    # 导入的写意图**不恢复**：pending_pr 完全来自请求体，若照单全收，任何持 API_KEY 的
    # 调用方都能「导入一个提案 → 直接 apply-fix」，用仓库 token 推任意文件并开 PR，
    # 绕过 Agent 调查与人工确认。导入后必须重新跑一次调查才会再次产生提案。
    if body.get("pending_pr"):
        logger.warning(
            "Ignoring pending_pr from imported payload for session %s: write intent must be regenerated",
            new_session.session_id,
        )

    # 导入事件历史：校验每个事件的 type 字段，跳过无效项避免 KeyError 导致整个导入中途失败。
    # 限制事件数量防止恶意超大 payload 长时间阻塞写操作；批量单事务写入（旧实现逐条
    # INSERT+commit，5MB 上限导入最多 5000 次事务，长时间占住 SQLite 写锁）。
    events = body.get("events")
    if isinstance(events, list):
        max_events = 5000
        valid_events = [event for event in events[:max_events] if isinstance(event, dict) and event.get("type")]
        if valid_events:
            await manager.append_events(new_session.session_id, valid_events)

    refreshed = await manager.get(new_session.session_id)
    if refreshed is None:
        raise HTTPException(status_code=500, detail="Imported session could not be loaded")
    return session_summary(refreshed)
