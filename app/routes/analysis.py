"""Investigation endpoints: one-shot analysis (``POST /analyze``) and SSE streaming (``POST /stream``)."""

import asyncio
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
    build_issue_agent,
    resolve_override_settings,
)
from app.events import cancelled_event, error_event, session_event
from app.github import GitHubError, GitHubRateLimitError, GitHubResourceError
from app.models import AnalysisReport, AnalyzeRequest, StreamRequest
from app.services import (
    BUSY_INVESTIGATIONS,
    SESSION_ALREADY_RUNNING,
    acquire_investigation_slot,
    event_payload,
    finish_cancelled_session,
    mark_stream_interrupted,
    record_agent_event,
)
from app.sessions import Session, SessionConflictError
from app.sse import _HeartbeatSentinel, _iter_events_with_heartbeat

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/analyze", response_model=AnalysisReport)
async def analyze(
    body: AnalyzeRequest,
    clients: ProviderClientsDep,
    breaker: CircuitBreakerDep,
    slots: InvestigationSlots,
) -> AnalysisReport:
    # 并发闸门：限流只按请求数计，一次调查却可能跑满 investigation_timeout，
    # 因此还要限制同时在跑的调查数量（否则成本与资源没有上界）。
    if not await acquire_investigation_slot(slots):
        raise HTTPException(status_code=429, detail=BUSY_INVESTIGATIONS, headers={"Retry-After": "30"})
    agent = build_issue_agent(clients, resolve_override_settings(body), breaker)
    try:
        return await agent.investigate(str(body.issue_url))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GitHubRateLimitError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except GitHubResourceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except GitHubError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except APIError as error:
        logger.exception("Model API request failed")
        raise HTTPException(status_code=502, detail="Model API request failed") from error
    except ModelResponseError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    finally:
        await agent.aclose()
        slots.release()


@router.post("/stream")
async def stream_analysis(
    body: StreamRequest,
    clients: ProviderClientsDep,
    session_mgr: SessionMgr,
    breaker: CircuitBreakerDep,
    slots: InvestigationSlots,
) -> StreamingResponse:
    settings = resolve_override_settings(body)
    agent = build_issue_agent(clients, settings, breaker)

    async def event_generator() -> AsyncIterator[str]:
        session: Session | None = None
        started_at = monotonic()
        slot_held = False
        try:
            # 并发闸门先行：拿不到名额立刻回一条可行动的 error 事件，而不是排队等
            # 前面的调查跑完（一次 /stream 最长 investigation_timeout = 10 分钟）。
            if not await acquire_investigation_slot(slots):
                yield error_event(BUSY_INVESTIGATIONS).to_sse()
                return
            slot_held = True

            if body.session_id:
                # 原子占位（会话调查租约）：同一会话同时只允许一个调查在跑。否则第二个
                # 请求会 clear_events() 删掉第一个正在跑的事件史，并在乐观锁上互相踩踏。
                session = await session_mgr.try_claim_running(body.session_id, phase="starting")
                if session is None:
                    if await session_mgr.get(body.session_id) is None:
                        yield error_event("Session not found").to_sse()
                    else:
                        yield error_event(SESSION_ALREADY_RUNNING).to_sse()
                    return
                # 续跑：清空上一轮残留事件/报告/指标，从干净状态重新调查，
                # 避免时间线重复或残留失败态。status/issue 等基本信息保留。
                await session_mgr.clear_events(session.session_id)
                refreshed = await session_mgr.get(session.session_id)
                if refreshed is None:
                    yield error_event("Session not found").to_sse()
                    return
                session = refreshed
            else:
                session = await session_mgr.create(str(body.issue_url))
                claimed = await session_mgr.try_claim_running(session.session_id, phase="starting")
                if claimed is None:
                    yield error_event(SESSION_ALREADY_RUNNING).to_sse()
                    return
                session = claimed

            created_event = session_event(session.session_id)
            await record_agent_event(session_mgr, session, created_event, started_at)
            yield created_event.to_sse()

            event_stream = agent.investigate_stream(session.issue_url, session=session)
            heartbeats = _iter_events_with_heartbeat(event_stream, timeout=15.0)
            try:
                async for item in heartbeats:
                    if isinstance(item, _HeartbeatSentinel):
                        # SSE 心跳：防止 nginx 等反向代理因空闲超时断开连接。
                        # 注意：绝不能取消正在执行的生成器步骤（见 _iter_events_with_heartbeat）。
                        yield ": keepalive\n\n"
                        # 轻量刷新活跃度，避免真实运行中的会话被 periodic stale recovery 误判为孤儿。
                        if session is not None:
                            try:
                                await session_mgr.touch(session.session_id)
                            except Exception:
                                logger.debug("touch failed for session %s", session.session_id)
                        continue
                    event = item
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
                # 显式关闭心跳包装器（而非等 GC）：提前 return 的取消分支也必须让
                # 被取消的调查立刻停下，否则它还会跑完当前网络步骤并继续计费。
                await heartbeats.aclose()
                await event_stream.aclose()

            async with session.lock:
                session.status = "completed"
                session.phase = "completed"
                session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)
                await session_mgr.save(session)
            logger.info("Session %s completed with metrics %s", session.session_id, session.metrics)
        except asyncio.CancelledError:
            if session is not None:
                try:
                    await mark_stream_interrupted(session_mgr, session.session_id, started_at)
                except Exception:
                    logger.exception("Failed to mark stream interrupted for session %s", session.session_id)
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
            # 详细堆栈进日志，客户端只拿脱敏后的友好消息
            logger.exception("Stream failed for session %s", session.session_id if session else "unknown")
            friendly = friendly_chat_error(exc)
            if session is not None:
                async with session.lock:
                    session.status = "failed"
                    session.phase = "failed"
                    session.error_message = friendly[:500]
                    session.metrics["duration_ms"] = round((monotonic() - started_at) * 1000)
                    try:
                        await session_mgr.save(session)
                    except SessionConflictError:
                        logger.warning(
                            "Could not persist failure state for concurrently updated session %s",
                            session.session_id,
                        )
            failure = error_event(friendly)
            if session is not None:
                await session_mgr.append_event(session.session_id, event_payload(failure))
            yield failure.to_sse()
        finally:
            await agent.aclose()
            if slot_held:
                slots.release()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
