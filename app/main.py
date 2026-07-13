import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from openai import APIError

from app.agent import IssueAgent, ModelResponseError
from app.auth import AuthMiddleware
from app.config import get_settings
from app.events import error_event
from app.github import GitHubClient, GitHubError, GitHubRateLimitError
from app.models import (
    AnalysisReport,
    AnalyzeRequest,
    ApplyFixRequest,
    ChatRequest,
    ChatResponse,
    CreatePRResponse,
    StreamRequest,
)
from app.sessions import SessionManager

logger = logging.getLogger(__name__)
app = FastAPI(title="GitHub Issue Agent", version="0.3.0")
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
        try:
            if request.session_id:
                session = await session_mgr.get(request.session_id)
                if session is None:
                    yield error_event("Session not found").to_sse()
                    return
                async for event in agent.investigate_stream("", session=session):
                    yield event.to_sse()
                await session_mgr.save(session)
            else:
                async for event in agent.investigate_stream(str(request.issue_url)):
                    yield event.to_sse()
        except Exception as exc:
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
    try:
        if request.session_id:
            session = await session_mgr.get(request.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")
            result = await agent.chat(session, request.message)
            await session_mgr.save(session)
            return result

        if request.issue_url is None:
            raise HTTPException(status_code=422, detail="issue_url is required to start a new session")

        session = await session_mgr.create(str(request.issue_url))
        report = await agent.investigate(str(request.issue_url), session=session)
        await session_mgr.save(session)
        return ChatResponse(
            session_id=session.session_id,
            reply=format_report_text(report),
            tools_used=[],
            report=report,
        )
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

    async with GitHubClient(settings.github_token) as github:
        branch = proposal["branch"]
        base = session.issue.default_branch if session.issue else "main"

        # Create branch
        result = await github.create_branch(
            session.issue.owner, session.issue.repo, branch,
            proposal.get("base_sha", base),
        )

        # Apply each file change
        for change in proposal.get("changes", []):
            await github.create_or_update_file(
                session.issue.owner, session.issue.repo,
                change["path"], change["content"], branch, change.get("message", "fix: apply patch"),
            )

        # Create PR
        pr = await github.create_pull_request(
            session.issue.owner, session.issue.repo,
            branch, base, proposal["title"], proposal["body"],
        )
        return CreatePRResponse(pr_url=pr["pr_url"], branch=branch)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if templates is None:
        raise HTTPException(status_code=404, detail="Web UI templates not found")
    return templates.TemplateResponse("index.html", {"request": request})


def format_report_text(report: AnalysisReport) -> str:
    lines = [
        f"Summary: {report.summary}", "",
        f"Root Cause: {report.root_cause}", "",
        f"Confidence: {report.confidence}",
    ]
    if report.evidence:
        lines.append(""); lines.append("Code Evidence:")
        for ev in report.evidence:
            lines.append(f"  - {ev.path} {ev.lines or ''}: {ev.reason or ''}")
    if report.proposed_changes:
        lines.append(""); lines.append("Proposed Changes:")
        for i, change in enumerate(report.proposed_changes, 1):
            lines.append(f"  {i}. {change}")
    if report.patch:
        lines.append(""); lines.append("Patch:"); lines.append(report.patch)
    if report.tests:
        lines.append(""); lines.append("Suggested Tests:")
        for i, test in enumerate(report.tests, 1):
            lines.append(f"  {i}. {test}")
    if report.risks:
        lines.append(""); lines.append("Risks:")
        for risk in report.risks:
            lines.append(f"  - {risk}")
    return "\n".join(lines)
