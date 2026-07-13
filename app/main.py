import logging

from fastapi import FastAPI, HTTPException
from openai import APIError

from app.agent import IssueAgent, ModelResponseError
from app.config import get_settings
from app.github import GitHubError, GitHubRateLimitError
from app.models import AnalysisReport, AnalyzeRequest, ChatRequest, ChatResponse
from app.sessions import SessionManager

logger = logging.getLogger(__name__)
app = FastAPI(title="GitHub Issue Agent", version="0.2.0")
session_manager = SessionManager()


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
            raise HTTPException(
                status_code=422,
                detail="issue_url is required to start a new session",
            )

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


def format_report_text(report: AnalysisReport) -> str:
    lines = [
        f"摘要：{report.summary}",
        "",
        f"根因：{report.root_cause}",
        "",
        f"置信度：{report.confidence}",
    ]
    if report.evidence:
        lines.append("")
        lines.append("代码证据：")
        for ev in report.evidence:
            lines.append(f"  - {ev.path} {ev.lines or ''}: {ev.reason or ''}")
    if report.proposed_changes:
        lines.append("")
        lines.append("修复建议：")
        for i, change in enumerate(report.proposed_changes, 1):
            lines.append(f"  {i}. {change}")
    if report.patch:
        lines.append("")
        lines.append("补丁：")
        lines.append(report.patch)
    if report.tests:
        lines.append("")
        lines.append("建议测试：")
        for i, test in enumerate(report.tests, 1):
            lines.append(f"  {i}. {test}")
    if report.risks:
        lines.append("")
        lines.append("风险提示：")
        for risk in report.risks:
            lines.append(f"  - {risk}")
    return "\n".join(lines)
