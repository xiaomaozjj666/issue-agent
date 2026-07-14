import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from openai import APIError

from app.agent import IssueAgent, ModelResponseError
from app.auth import AuthMiddleware
from app.config import get_settings
from app.events import error_event, session_event
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
from app.sessions import Session, SessionManager

logger = logging.getLogger(__name__)
app = FastAPI(title="GitHub Issue Agent", version="0.3.1")
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
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR)) if _TEMPLATES_DIR.exists() else None


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
        try:
            if request.session_id:
                session = await session_mgr.get(request.session_id)
                if session is None:
                    yield error_event("Session not found").to_sse()
                    return
            else:
                session = await session_mgr.create(str(request.issue_url))

            session.status = "running"
            session.error_message = None
            await session_mgr.save(session)
            yield session_event(session.session_id).to_sse()
            async with session.lock:
                async for event in agent.investigate_stream(session.issue_url, session=session):
                    if event.type == "start":
                        await session_mgr.save(session)
                    yield event.to_sse()
                session.status = "completed"
                await session_mgr.save(session)
        except Exception as exc:
            if session is not None:
                session.status = "failed"
                session.error_message = str(exc)[:500]
                await session_mgr.save(session)
            yield error_event(str(exc)).to_sse()
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
            session.error_message = None
            await session_mgr.save(session)
            result = await agent.chat(session, request.message)
            session.status = "completed"
            await session_mgr.save(session)
            return result

        if request.issue_url is None:
            raise HTTPException(status_code=422, detail="issue_url is required to start a new session")

        session = await session_mgr.create(str(request.issue_url))
        session.status = "running"
        await session_mgr.save(session)
        report = await agent.investigate(str(request.issue_url), session=session)
        session.status = "completed"
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
    sessions = await _get_session_manager().list(
        archived=archived,
        query=q,
        limit=limit,
    )
    return [_session_summary(session) for session in sessions]


@app.get("/session/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str) -> SessionDetail:
    session = await _get_session_manager().get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionDetail(
        **_session_summary(session).model_dump(),
        messages=session.messages,
        report=session.report,
    )


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
        async with GitHubClient(settings.github_token, write_enabled=True) as github:
            branch = proposal["branch"]
            base = session.issue.default_branch
            base_sha = await github.get_branch_sha(session.issue.owner, session.issue.repo, base)

            await github.create_branch(session.issue.owner, session.issue.repo, branch, base_sha)

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
        logger.exception("Failed to apply fix for session %s", session_id)
        raise HTTPException(status_code=502, detail=str(error)) from error


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
        error_message=session.error_message,
        archived=session.archived_at is not None,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


async def _mark_session_failed(manager: SessionManager, session: Session | None, error: Exception) -> None:
    if session is None:
        return
    session.status = "failed"
    session.error_message = str(error)[:500]
    await manager.save(session)
