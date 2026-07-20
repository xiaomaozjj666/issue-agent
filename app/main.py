"""FastAPI 入口：只保留 HTTP 端点定义和请求/响应组装。

业务逻辑（会话状态更新、报告格式化、PR 应用与回滚、事件持久化）已抽到
app.services，main.py 通过函数式接口调用，避免 HTTP 层耦合业务细节。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openai import APIError

from app.agent import IssueAgent, ModelResponseError
from app.auth import AuthMiddleware
from app.build import BUILD_ID
from app.config import get_settings
from app.events import cancelled_event, error_event, session_event
from app.github import GitHubError, GitHubRateLimitError
from app.i18n import get_frontend_strings
from app.logging_config import setup_logging
from app.models import (
    AnalysisReport,
    AnalyzeRequest,
    ApplyFixRequest,
    ChatRequest,
    ChatResponse,
    CreatePRResponse,
    SessionDetail,
    SessionEventRecord,
    SessionSummary,
    SessionUpdateRequest,
    StreamRequest,
)
from app.services import (
    apply_fix,
    event_payload,
    finish_cancelled_session,
    format_report_text,
    mark_session_failed,
    mark_stream_interrupted,
    record_agent_event,
    session_summary,
)
from app.sessions import Session, SessionConflictError, SessionManager

logger = logging.getLogger(__name__)
_session_manager: SessionManager | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    try:
        yield
    finally:
        global _session_manager
        manager = _session_manager
        _session_manager = None
        if manager is not None:
            await manager.close()


app = FastAPI(title="GitHub Issue Agent", version="0.6.0", lifespan=lifespan)
app.add_middleware(AuthMiddleware)


def _get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        db_path: str | None = get_settings().session_db_path
        if db_path == ":memory:":
            db_path = None
        _session_manager = SessionManager(db_path=db_path)
    return _session_manager


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR)) if _TEMPLATES_DIR.exists() else None
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.exception_handler(SessionConflictError)
async def session_conflict_handler(request: Request, error: SessionConflictError) -> JSONResponse:
    logger.warning("Session conflict on %s: %s", request.url.path, error)
    return JSONResponse(status_code=409, content={"detail": "Session changed concurrently; reload and try again"})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "issue-agent", "build_id": BUILD_ID}


@app.post("/analyze", response_model=AnalysisReport)
async def analyze(request: AnalyzeRequest) -> AnalysisReport:
    agent = IssueAgent(get_settings())
    try:
        return await agent.investigate(str(request.issue_url))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GitHubRateLimitError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except GitHubError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except APIError as error:
        logger.exception("Model API request failed")
        raise HTTPException(status_code=502, detail="Model API request failed") from error
    except ModelResponseError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    finally:
        await agent.aclose()


@app.post("/stream")
async def stream_analysis(request: StreamRequest) -> StreamingResponse:
    settings = get_settings()
    agent = IssueAgent(settings)
    session_mgr = _get_session_manager()

    async def event_generator() -> AsyncIterator[str]:
        session: Session | None = None
        started_at = monotonic()
        try:
            if request.session_id:
                session = await session_mgr.get(request.session_id)
                if session is None:
                    yield error_event("Session not found").to_sse()
                    return
            else:
                session = await session_mgr.create(str(request.issue_url))

            # 状态写入临界区：只在 session 状态切换时短暂持锁，不在 yield 期间持锁。
            # 这样调查进行时同一 session 的 PATCH/DELETE/chat 不会被 SSE 网络 I/O 阻塞。
            async with session.lock:
                session.status = "running"
                session.phase = "starting"
                session.cancel_requested = False
                session.error_message = None
                await session_mgr.save(session)
            created_event = session_event(session.session_id)
            await record_agent_event(session_mgr, session, created_event, started_at)
            yield created_event.to_sse()

            event_stream = agent.investigate_stream(session.issue_url, session=session)
            try:
                async for event in event_stream:
                    if await session_mgr.is_cancel_requested(session.session_id):
                        await event_stream.aclose()
                        cancelled = cancelled_event()
                        await finish_cancelled_session(session_mgr, session.session_id, cancelled, started_at)
                        yield cancelled.to_sse()
                        return
                    await record_agent_event(session_mgr, session, event, started_at)
                    yield event.to_sse()
            finally:
                # 显式关闭生成器，确保内部的 async with GitHubClient 在所有路径都正确清理。
                # aclose() 会触发生成器的 finally 块执行 GitHubClient.__aexit__。
                await event_stream.aclose()

            async with session.lock:
                session.status = "completed"
                session.phase = "completed"
                session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)
                await session_mgr.save(session)
            logger.info("Session %s completed with metrics %s", session.session_id, session.metrics)
        except asyncio.CancelledError:
            # 客户端断开：标记 session 为 interrupted，但内部任何异常都不能替换 CancelledError，
            # 否则破坏 asyncio 任务取消传播链。mark_stream_interrupted 内部已捕获 SessionConflictError。
            if session is not None:
                await mark_stream_interrupted(session_mgr, session.session_id, started_at)
            raise
        except SessionConflictError:
            logger.warning("Concurrent update detected for session %s", session.session_id if session else "unknown")
            if session is not None and await session_mgr.is_cancel_requested(session.session_id):
                cancelled = cancelled_event()
                await finish_cancelled_session(session_mgr, session.session_id, cancelled, started_at)
                yield cancelled.to_sse()
                return
            yield error_event("This session changed in another process; reload it before continuing").to_sse()
        except Exception as exc:
            if session is not None:
                async with session.lock:
                    session.status = "failed"
                    session.phase = "failed"
                    session.error_message = str(exc)[:500]
                    session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)
                    try:
                        await session_mgr.save(session)
                    except SessionConflictError:
                        logger.warning(
                            "Could not persist failure state for concurrently updated session %s",
                            session.session_id,
                        )
            failure = error_event(str(exc))
            if session is not None:
                await session_mgr.append_event(session.session_id, event_payload(failure))
            yield failure.to_sse()
        finally:
            await agent.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    agent = IssueAgent(get_settings())
    session_mgr = _get_session_manager()
    session: Session | None = None
    try:
        if request.session_id:
            session = await session_mgr.get(request.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")
            if session.archived_at is not None:
                raise HTTPException(status_code=409, detail="Restore the archived session before continuing")
            session.status = "running"
            session.phase = "chatting"
            session.error_message = None
            await session_mgr.save(session)
            result = await agent.chat(session, request.message)
            session.status = "completed"
            session.phase = "completed"
            await session_mgr.save(session)
            return result

        if request.issue_url is None:
            raise HTTPException(status_code=422, detail="issue_url is required to start a new session")

        session = await session_mgr.create(str(request.issue_url))
        session.status = "running"
        session.phase = "investigating"
        await session_mgr.save(session)
        report = await agent.investigate(str(request.issue_url), session=session)
        session.status = "completed"
        session.phase = "completed"
        await session_mgr.save(session)
        return ChatResponse(
            session_id=session.session_id,
            reply=format_report_text(report),
            tools_used=[],
            report=report,
        )
    except ValueError as error:
        await mark_session_failed(session_mgr, session, error)
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GitHubRateLimitError as error:
        await mark_session_failed(session_mgr, session, error)
        raise HTTPException(status_code=429, detail=str(error)) from error
    except GitHubError as error:
        await mark_session_failed(session_mgr, session, error)
        raise HTTPException(status_code=502, detail=str(error)) from error
    except APIError as error:
        await mark_session_failed(session_mgr, session, error)
        logger.exception("Model API request failed")
        raise HTTPException(status_code=502, detail="Model API request failed") from error
    except ModelResponseError as error:
        await mark_session_failed(session_mgr, session, error)
        raise HTTPException(status_code=502, detail=str(error)) from error
    finally:
        await agent.aclose()


@app.get("/sessions", response_model=list[SessionSummary])
async def list_sessions(
    archived: bool = False,
    q: str = Query(default="", max_length=160),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[SessionSummary]:
    manager = _get_session_manager()
    cutoff = datetime.now(UTC) - timedelta(seconds=get_settings().session_stale_after_seconds)
    recovered = await manager.recover_stale(cutoff.isoformat(timespec="seconds"))
    if recovered:
        logger.warning("Recovered %d stale running session(s)", recovered)
    sessions = await manager.list(
        archived=archived,
        query=q,
        limit=limit,
    )
    return [session_summary(session) for session in sessions]


@app.get("/session/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str) -> SessionDetail:
    manager = _get_session_manager()
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionDetail(
        **session_summary(session).model_dump(),
        messages=session.messages,
        report=session.report,
        events=[SessionEventRecord.model_validate(event) for event in await manager.list_events(session_id)],
    )


@app.post("/session/{session_id}/cancel", response_model=SessionSummary)
async def cancel_session(session_id: str) -> SessionSummary:
    manager = _get_session_manager()
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


@app.patch("/session/{session_id}", response_model=SessionSummary)
async def update_session(session_id: str, request: SessionUpdateRequest) -> SessionSummary:
    manager = _get_session_manager()
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


@app.delete("/session/{session_id}", status_code=204)
async def delete_session(session_id: str) -> Response:
    manager = _get_session_manager()
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    async with session.lock:
        await manager.delete(session_id)
    return Response(status_code=204)


@app.get("/session/{session_id}/report", response_model=AnalysisReport)
async def get_session_report(session_id: str) -> AnalysisReport:
    session = await _get_session_manager().get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.report is None:
        raise HTTPException(status_code=404, detail="Report not yet generated for this session")
    return session.report


@app.post("/session/{session_id}/apply-fix", response_model=CreatePRResponse)
async def apply_fix_route(session_id: str, request: ApplyFixRequest) -> CreatePRResponse:
    return await apply_fix(
        session_id,
        request,
        settings=get_settings(),
        session_mgr=_get_session_manager(),
    )


@app.post("/apply-fix", response_model=CreatePRResponse, include_in_schema=False, deprecated=True)
async def apply_fix_legacy_route(request: ApplyFixRequest, session_id: str) -> CreatePRResponse:
    """Compatibility route for clients created before the session-scoped endpoint."""
    return await apply_fix(
        session_id,
        request,
        settings=get_settings(),
        session_mgr=_get_session_manager(),
    )


@app.get("/session/{session_id}/proposal")
async def get_pr_proposal(session_id: str) -> dict:
    manager = _get_session_manager()
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
        "changes": [
            {
                "path": change["path"],
                "message": change["message"],
                "proposed_lines": len(str(change.get("content", "")).splitlines()),
            }
            for change in proposal.get("changes", [])
        ],
    }


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if templates is None:
        raise HTTPException(status_code=404, detail="Web UI templates not found")
    settings = get_settings()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "language": settings.language,
            "frontend_strings": get_frontend_strings(settings.language),
            "build_id": BUILD_ID,
        },
    )
