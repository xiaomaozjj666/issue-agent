import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openai import APIError

from app.agent import IssueAgent, ModelResponseError
from app.auth import AuthMiddleware
from app.config import get_settings
from app.events import error_event
from app.github import GitHubError, GitHubRateLimitError
from app.models import AnalysisReport, AnalyzeRequest, ChatRequest, ChatResponse, StreamRequest
from app.sessions import SessionManager

logger = logging.getLogger(__name__)
app = FastAPI(title="GitHub Issue Agent", version="0.3.0")
app.add_middleware(AuthMiddleware)

session_manager = SessionManager()

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
    """SSE endpoint — streams AgentEvent objects as they happen."""
    settings = get_settings()
    agent = IssueAgent(settings)

    async def event_generator():
        try:
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
    try:
        if request.session_id:
            session = session_manager.get(request.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")
            return await agent.chat(session, request.message)

        if request.issue_url is None:
            raise HTTPException(status_code=422, detail="issue_url is required to start a new session")

        session = session_manager.create(str(request.issue_url))
        report = await agent.investigate(str(request.issue_url), session=session)
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
