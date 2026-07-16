import asyncio
import logging
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
from app.config import get_settings
from app.events import AgentEvent, cancelled_event, error_event, session_event
from app.github import GitHubClient, GitHubError, GitHubRateLimitError
from app.models import (
    AnalysisReport,
    AnalyzeRequest,
    ApplyFixRequest,
    ChatRequest,
    ChatResponse,
    CreatePRResponse,
    SessionDetail,
    SessionSummary,
    SessionUpdateRequest,
    StreamRequest,
)
from app.sessions import Session, SessionConflictError, SessionManager
from app.tools import validate_pr_proposal

logger = logging.getLogger(__name__)
app = FastAPI(title="GitHub Issue Agent", version="0.4.0")
app.add_middleware(AuthMiddleware)

_session_manager: SessionManager | None = None


def _get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        db_path = get_settings().session_db_path
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
    return {"status": "ok"}


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

    async def event_generator():
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

            session.status = "running"
            session.phase = "starting"
            session.cancel_requested = False
            session.error_message = None
            await session_mgr.save(session)
            created_event = session_event(session.session_id)
            await _record_agent_event(session_mgr, session, created_event, started_at)
            yield created_event.to_sse()
            async with session.lock:
                event_stream = agent.investigate_stream(session.issue_url, session=session)
                async for event in event_stream:
                    if await session_mgr.is_cancel_requested(session.session_id):
                        await event_stream.aclose()
                        cancelled = cancelled_event()
                        await _finish_cancelled_session(session_mgr, session.session_id, cancelled, started_at)
                        yield cancelled.to_sse()
                        return
                    await _record_agent_event(session_mgr, session, event, started_at)
                    yield event.to_sse()
                session.status = "completed"
                session.phase = "completed"
                session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)
                await session_mgr.save(session)
                logger.info("Session %s completed with metrics %s", session.session_id, session.metrics)
        except asyncio.CancelledError:
            if session is not None:
                await _mark_stream_interrupted(session_mgr, session.session_id, started_at)
            raise
        except SessionConflictError:
            logger.warning("Concurrent update detected for session %s", session.session_id if session else "unknown")
            if session is not None and await session_mgr.is_cancel_requested(session.session_id):
                cancelled = cancelled_event()
                await _finish_cancelled_session(session_mgr, session.session_id, cancelled, started_at)
                yield cancelled.to_sse()
                return
            yield error_event("This session changed in another process; reload it before continuing").to_sse()
        except Exception as exc:
            if session is not None:
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
                await session_mgr.append_event(session.session_id, _event_payload(failure))
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
        await _mark_session_failed(session_mgr, session, error)
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GitHubRateLimitError as error:
        await _mark_session_failed(session_mgr, session, error)
        raise HTTPException(status_code=429, detail=str(error)) from error
    except GitHubError as error:
        await _mark_session_failed(session_mgr, session, error)
        raise HTTPException(status_code=502, detail=str(error)) from error
    except APIError as error:
        await _mark_session_failed(session_mgr, session, error)
        logger.exception("Model API request failed")
        raise HTTPException(status_code=502, detail="Model API request failed") from error
    except ModelResponseError as error:
        await _mark_session_failed(session_mgr, session, error)
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
    return [_session_summary(session) for session in sessions]


@app.get("/session/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str) -> SessionDetail:
    manager = _get_session_manager()
    session = await manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionDetail(
        **_session_summary(session).model_dump(),
        messages=session.messages,
        report=session.report,
        events=await manager.list_events(session_id),
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
    return _session_summary(refreshed)


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
    return _session_summary(session)


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


@app.post("/apply-fix", response_model=CreatePRResponse)
async def apply_fix(request: ApplyFixRequest, session_id: str) -> CreatePRResponse:
    settings = get_settings()
    if not settings.write_mode:
        raise HTTPException(status_code=403, detail="Write mode is disabled")

    session_mgr = _get_session_manager()
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
    return templates.TemplateResponse(request=request, name="index.html")


def format_report_text(report: AnalysisReport) -> str:
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
    return "\n".join(lines)


def _session_summary(session: Session) -> SessionSummary:
    owner = session.issue.owner if session.issue else ""
    repo = session.issue.repo if session.issue else ""
    issue_number = session.issue.number if session.issue else None
    fallback_title = session.issue.title if session.issue else session.issue_url
    return SessionSummary(
        session_id=session.session_id,
        issue_url=session.issue_url,
        owner=owner,
        repo=repo,
        issue_number=issue_number,
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


async def _mark_session_failed(manager: SessionManager, session: Session | None, error: Exception) -> None:
    if session is None:
        return
    session.status = "failed"
    session.phase = "failed"
    session.error_message = str(error)[:500]
    try:
        await manager.save(session)
    except SessionConflictError:
        logger.warning("Could not mark concurrently updated session %s as failed", session.session_id)


def _event_payload(event: AgentEvent) -> dict:
    return {"type": event.type, "data": event.data, "message": event.message}


async def _record_agent_event(
    manager: SessionManager,
    session: Session,
    event: AgentEvent,
    started_at: float,
) -> None:
    await manager.append_event(session.session_id, _event_payload(event))
    if event.type == "phase" and event.data:
        session.phase = str(event.data.get("phase", session.phase))
    session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)
    await manager.save(session)


async def _finish_cancelled_session(
    manager: SessionManager,
    session_id: str,
    event: AgentEvent,
    started_at: float,
) -> None:
    session = await manager.get(session_id)
    if session is None:
        return
    session.status = "cancelled"
    session.phase = "cancelled"
    session.error_message = None
    session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)
    await manager.append_event(session_id, _event_payload(event))
    await manager.save(session)


async def _mark_stream_interrupted(manager: SessionManager, session_id: str, started_at: float) -> None:
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
