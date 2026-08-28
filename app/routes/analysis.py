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
    ProviderClientsDep,
    SessionMgr,
    build_issue_agent,
    resolve_override_settings,
)
from app.events import cancelled_event, error_event, session_event
from app.github import GitHubError, GitHubRateLimitError, GitHubResourceError
from app.models import AnalysisReport, AnalyzeRequest, StreamRequest
from app.services import (
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
    body: AnalyzeRequest, clients: ProviderClientsDep, breaker: CircuitBreakerDep
) -> AnalysisReport:
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


@router.post("/stream")
async def stream_analysis(
    body: StreamRequest, clients: ProviderClientsDep, session_mgr: SessionMgr, breaker: CircuitBreakerDep
) -> StreamingResponse:
    settings = resolve_override_settings(body)
    agent = build_issue_agent(clients, settings, breaker)

    async def event_generator() -> AsyncIterator[str]:
        session: Session | None = None
        started_at = monotonic()
        try:
            if body.session_id:
                session = await session_mgr.get(body.session_id)
                if session is None:
                    yield error_event("Session not found").to_sse()
                    return
                # 续跑：清空上一轮残留事件/报告/指标，从干净状态重新调查，
                # 避免时间线重复或残留失败态。status/issue 等基本信息保留。
                await session_mgr.clear_events(session.session_id)
            else:
                session = await session_mgr.create(str(body.issue_url))

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
                async for item in _iter_events_with_heartbeat(event_stream, timeout=15.0):
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

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
