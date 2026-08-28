"""Chat endpoints: blocking (``POST /chat``) and SSE token streaming (``POST /chat/stream``)."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from time import monotonic

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from openai import APIError

from app.agent import ModelResponseError, friendly_chat_error
from app.deps import (
    CircuitBreakerDep,
    ProviderClientsDep,
    SessionMgr,
    apply_regenerate,
    build_issue_agent,
    resolve_override_settings,
)
from app.github import GitHubError, GitHubRateLimitError, GitHubResourceError
from app.models import ChatRequest, ChatResponse
from app.services import format_report_text, mark_session_failed, mark_stream_interrupted
from app.sessions import Session
from app.sse import _HeartbeatSentinel, _iter_events_with_heartbeat

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest, clients: ProviderClientsDep, session_mgr: SessionMgr, breaker: CircuitBreakerDep
) -> ChatResponse:
    agent = build_issue_agent(clients, resolve_override_settings(body), breaker)
    session: Session | None = None
    try:
        if body.session_id:
            session = await session_mgr.get(body.session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")
            if session.archived_at is not None:
                raise HTTPException(status_code=409, detail="Restore the archived session before continuing")
            async with session.lock:
                session.status = "running"
                session.phase = "chatting"
                session.error_message = None
                if body.regenerate:
                    apply_regenerate(session, body)
                await session_mgr.save(session)
            result = await agent.chat(session, body.message)
            async with session.lock:
                session.status = "completed"
                session.phase = "completed"
                await session_mgr.save(session)
            return result

        if body.issue_url is None:
            raise HTTPException(status_code=422, detail="issue_url is required to start a new session")

        session = await session_mgr.create(str(body.issue_url))
        # 新会话状态切换与 /stream 保持一致：状态写入持锁，
        # 调查执行（investigate 内部不持锁）期间允许 PATCH/DELETE/cancel 并发操作。
        async with session.lock:
            session.status = "running"
            session.phase = "investigating"
            await session_mgr.save(session)
        report = await agent.investigate(str(body.issue_url), session=session)
        async with session.lock:
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
    except GitHubResourceError as error:
        await mark_session_failed(session_mgr, session, error)
        raise HTTPException(status_code=422, detail=str(error)) from error
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


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest, clients: ProviderClientsDep, session_mgr: SessionMgr, breaker: CircuitBreakerDep
) -> StreamingResponse:
    """Stream chat reply token-by-token via Server-Sent Events.

    SSE event shapes (``data: <json>\\n\\n``):
        {"type": "delta", "content": "..."}        # incremental content chunk
        {"type": "tool_call", "name": "...", "args": {...}}  # tool invocation notification
        {"type": "tool_result", "name": "...", "preview": "..."}  # tool result summary
        {"type": "done", "reply": "...", "tools_used": [...]}
        {"type": "error", "message": "..."}

    Only the existing-session branch is supported here. New-session-via-chat
    falls back to the non-streaming ``POST /chat`` endpoint.
    """
    if not body.session_id:
        raise HTTPException(status_code=422, detail="session_id is required for streaming chat")
    session = await session_mgr.get(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.archived_at is not None:
        raise HTTPException(status_code=409, detail="Restore the archived session before continuing")

    agent = build_issue_agent(clients, resolve_override_settings(body), breaker)
    async with session.lock:
        session.status = "running"
        session.phase = "chatting"
        session.error_message = None
        if body.regenerate:
            apply_regenerate(session, body)
        await session_mgr.save(session)

    async def event_generator() -> AsyncIterator[str]:
        started_at = monotonic()
        chat_error_received = False
        try:
            chat_iter = agent.chat_stream(session, body.message).__aiter__()
            async for item in _iter_events_with_heartbeat(chat_iter, timeout=15.0):
                if isinstance(item, _HeartbeatSentinel):
                    yield ": keepalive\n\n"
                    continue
                event = item
                # 跟踪 error 事件：agent.chat_stream 吞掉异常并 yield error，
                # 不走 except 分支，需要据此决定最终状态
                if isinstance(event, dict) and event.get("type") == "error":
                    chat_error_received = True
                payload = json.dumps(event, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            # 流结束：根据是否收到 error 事件决定最终状态
            async with session.lock:
                session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)
                if chat_error_received:
                    session.status = "failed"
                    session.phase = "failed"
                    session.error_message = "Chat stream encountered an error"
                else:
                    session.status = "completed"
                    session.phase = "completed"
                await session_mgr.save(session)
        except asyncio.CancelledError:
            # 客户端断开连接（浏览器关闭/网络中断）：标记会话为中断，
            # 避免 session.status 永远卡在 "running" 且锁被持有
            logger.info("chat stream cancelled (client disconnect) for session %s", session.session_id)
            try:
                await mark_stream_interrupted(session_mgr, session.session_id, started_at)
            except Exception:
                logger.exception("Failed to mark chat stream interrupted for session %s", session.session_id)
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced to client via SSE
            logger.exception("chat stream failed for session %s", session.session_id)
            await mark_session_failed(session_mgr, session, exc)
            err_payload = json.dumps({"type": "error", "message": friendly_chat_error(exc)}, ensure_ascii=False)
            yield f"data: {err_payload}\n\n"
        finally:
            await agent.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
