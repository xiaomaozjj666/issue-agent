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
    InvestigationSlots,
    ProviderClientsDep,
    SessionMgr,
    apply_regenerate,
    build_issue_agent,
    resolve_override_settings,
)
from app.github import GitHubError, GitHubRateLimitError, GitHubResourceError
from app.models import ChatRequest, ChatResponse
from app.services import (
    BUSY_INVESTIGATIONS,
    SESSION_ALREADY_RUNNING,
    acquire_investigation_slot,
    format_report_text,
    mark_session_failed,
    mark_stream_interrupted,
    periodic_touch,
)
from app.sessions import Session
from app.sse import _HeartbeatSentinel, _iter_events_with_heartbeat

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    clients: ProviderClientsDep,
    session_mgr: SessionMgr,
    breaker: CircuitBreakerDep,
    slots: InvestigationSlots,
) -> ChatResponse:
    agent = build_issue_agent(clients, resolve_override_settings(body), breaker)
    session: Session | None = None
    slot_held = False
    try:
        session_id = body.session_id
        if session_id:
            session = await session_mgr.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail="Session not found")
            if session.archived_at is not None:
                raise HTTPException(status_code=409, detail="Restore the archived session before continuing")
            # 会话租约：同一会话同时只能有一个写者（调查或追问），否则两边的 save()
            # 会在乐观锁上互相踩踏，metrics 也会被叠加。
            claimed = await session_mgr.try_claim_running(session_id, phase="chatting")
            if claimed is None:
                raise HTTPException(status_code=409, detail=SESSION_ALREADY_RUNNING)
            session = claimed
            if body.regenerate:
                apply_regenerate(session, body)
                await session_mgr.save(session)
            # 阻塞式调用期间没有 SSE 心跳：没有这个心跳，超过 stale 阈值的调查会被
            # 后台恢复任务判成孤儿（终态丢失 + 用户拿到 409）。
            async with periodic_touch(session_mgr, session.session_id):
                result = await agent.chat(session, body.message)
            async with session.lock:
                session.status = "completed"
                session.phase = "completed"
                await session_mgr.save(session)
            return result

        if body.issue_url is None:
            raise HTTPException(status_code=422, detail="issue_url is required to start a new session")

        # 新会话 = 一次完整调查，占用并发闸门
        if not await acquire_investigation_slot(slots):
            raise HTTPException(status_code=429, detail=BUSY_INVESTIGATIONS, headers={"Retry-After": "30"})
        slot_held = True
        session = await session_mgr.create(str(body.issue_url))
        claimed = await session_mgr.try_claim_running(session.session_id, phase="investigating")
        if claimed is None:  # 新会话不可能已在运行；防御性回退
            raise HTTPException(status_code=409, detail=SESSION_ALREADY_RUNNING)
        session = claimed
        async with periodic_touch(session_mgr, session.session_id):
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
        if slot_held:
            await slots.release()


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    clients: ProviderClientsDep,
    session_mgr: SessionMgr,
    breaker: CircuitBreakerDep,
    slots: InvestigationSlots,
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
    session_id = body.session_id
    session = await session_mgr.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.archived_at is not None:
        raise HTTPException(status_code=409, detail="Restore the archived session before continuing")
    # 快速失败（只读检查，非原子）：保留 409 的 HTTP 语义；真正的原子占位放在生成器里，
    # 这样并发闸门名额的释放能落在同一个 try/finally 内，不会因提前返回而泄漏。
    if session.status == "running":
        raise HTTPException(status_code=409, detail=SESSION_ALREADY_RUNNING)

    agent = build_issue_agent(clients, resolve_override_settings(body), breaker)

    async def event_generator() -> AsyncIterator[str]:
        nonlocal session
        started_at = monotonic()
        chat_error_received = False
        slot_held = False
        try:
            if not await acquire_investigation_slot(slots):
                busy = json.dumps({"type": "error", "message": BUSY_INVESTIGATIONS}, ensure_ascii=False)
                yield f"data: {busy}\n\n"
                return
            slot_held = True
            # 原子占位：同一会话同时只允许一个写者（追问 / 调查），否则双方 save()
            # 会在乐观锁上互相踩踏，session.messages 也可能出现重复的 user 轮次。
            claimed = await session_mgr.try_claim_running(session_id, phase="chatting")
            if claimed is None:
                conflict = json.dumps({"type": "error", "message": SESSION_ALREADY_RUNNING}, ensure_ascii=False)
                yield f"data: {conflict}\n\n"
                return
            session = claimed
            assert session is not None  # claimed 已保证非 None；收窄类型供后续 save/touch 使用
            if body.regenerate:
                apply_regenerate(session, body)
                await session_mgr.save(session)

            chat_iter = agent.chat_stream(session, body.message).__aiter__()
            heartbeats = _iter_events_with_heartbeat(chat_iter, timeout=15.0)
            async for item in heartbeats:
                if isinstance(item, _HeartbeatSentinel):
                    yield ": keepalive\n\n"
                    # 心跳顺带刷新活跃时间（与 /stream 对齐）：否则长时间追问会被
                    # 后台 stale recovery 判成孤儿，抢走终态并把 409 甩给用户。
                    try:
                        await session_mgr.touch(session.session_id)
                    except Exception:
                        logger.debug("touch failed for session %s", session.session_id)
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
            # 避免 session.status 永远卡在 "running" 且锁被持有。
            # session 可能尚未拿到（闸门打满/占位失败就返回），因此这里必须判空。
            if session is not None:
                logger.info("chat stream cancelled (client disconnect) for session %s", session.session_id)
                try:
                    await mark_stream_interrupted(session_mgr, session.session_id, started_at)
                except Exception:
                    logger.exception("Failed to mark chat stream interrupted for session %s", session.session_id)
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced to client via SSE
            if session is not None:
                logger.exception("chat stream failed for session %s", session.session_id)
                await mark_session_failed(session_mgr, session, exc)
            err_payload = json.dumps({"type": "error", "message": friendly_chat_error(exc)}, ensure_ascii=False)
            yield f"data: {err_payload}\n\n"
        finally:
            await agent.aclose()
            if slot_held:
                await slots.release()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
