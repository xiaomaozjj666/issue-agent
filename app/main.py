"""FastAPI application entry-point: HTTP endpoint definitions and request/response assembly.

Business logic (session state updates, report formatting, PR apply/rollback,
event persistence) is delegated to ``app.services``.  This module only wires
HTTP concerns: routing, status codes, SSE streaming, and dependency injection.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: setup logging, initialize session store, purge stale data."""
    setup_logging()
    # Initialize session manager and attach to app state for DI
    settings = get_settings()
    db_path: str | None = settings.session_db_path
    if db_path == ":memory:":
        db_path = None
    manager = SessionManager(db_path=db_path)
    _app.state.session_manager = manager

    # Purge old completed/failed sessions on startup
    try:
        purged = await manager.purge_old_sessions(settings.session_retention_days)
        if purged:
            logger.info("Purged %d expired session(s) older than %d days", purged, settings.session_retention_days)
    except Exception:
        logger.warning("Session purge on startup failed; continuing", exc_info=True)

    try:
        yield
    finally:
        _app.state.session_manager = None
        await manager.close()


app = FastAPI(title="GitHub Issue Agent", version="0.6.0", lifespan=lifespan)
app.add_middleware(AuthMiddleware)


def get_session_manager(request: Request) -> SessionManager:
    """FastAPI dependency: retrieve the SessionManager from app state."""
    manager: SessionManager = request.app.state.session_manager
    return manager


# Annotated dependency alias — avoids B008 lint warnings and reduces line length.
SessionMgr = Annotated[SessionManager, Depends(get_session_manager)]


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
async def stream_analysis(request: StreamRequest, session_mgr: SessionMgr) -> StreamingResponse:
    settings = get_settings()
    agent = IssueAgent(settings)

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
                    # Persist session at key phase transitions to balance durability vs. write cost
                    if event.type in ("phase", "report", "done"):
                        await session_mgr.save(session)
                    # 工具调用事件时轻量刷新 metrics，让前端列表/详情实时显示调查进度
                    if event.type in ("tool_call", "tool_result"):
                        await session_mgr.update_metrics(session.session_id, session.metrics)
                    yield event.to_sse()
            finally:
                await event_stream.aclose()

            async with session.lock:
                session.status = "completed"
                session.phase = "completed"
                session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)
                await session_mgr.save(session)
            logger.info("Session %s completed with metrics %s", session.session_id, session.metrics)
        except asyncio.CancelledError:
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
async def chat(request: ChatRequest, session_mgr: SessionMgr) -> ChatResponse:
    agent = IssueAgent(get_settings())
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
    manager: SessionMgr,
    archived: bool = False,
    q: str = Query(default="", max_length=160),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[SessionSummary]:
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


@app.post("/session/{session_id}/cancel", response_model=SessionSummary)
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


@app.patch("/session/{session_id}", response_model=SessionSummary)
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


@app.delete("/session/{session_id}", status_code=204)
async def delete_session(session_id: str, manager: SessionMgr) -> Response:
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    async with session.lock:
        await manager.delete(session_id)
    return Response(status_code=204)


@app.get("/session/{session_id}/report", response_model=AnalysisReport)
async def get_session_report(session_id: str, manager: SessionMgr) -> AnalysisReport:
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.report is None:
        raise HTTPException(status_code=404, detail="Report not yet generated for this session")
    return session.report


@app.post("/session/{session_id}/apply-fix", response_model=CreatePRResponse)
async def apply_fix_route(session_id: str, request: ApplyFixRequest, session_mgr: SessionMgr) -> CreatePRResponse:
    return await apply_fix(
        session_id,
        request,
        settings=get_settings(),
        session_mgr=session_mgr,
    )


@app.post("/apply-fix", response_model=CreatePRResponse, include_in_schema=False, deprecated=True)
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


@app.get("/session/{session_id}/proposal")
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
