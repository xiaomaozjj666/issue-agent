"""SSE streaming helpers shared by the analysis (``/stream``) and chat (``/chat/stream``) endpoints."""

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import suppress
from typing import Any, TypeVar, cast


class _HeartbeatSentinel:
    """Marker yielded by ``_iter_events_with_heartbeat`` when a generator step is slow."""

    __slots__ = ()


_HEARTBEAT = _HeartbeatSentinel()

_T = TypeVar("_T")


async def _iter_events_with_heartbeat(
    event_iter: AsyncIterator[_T], *, timeout: float = 15.0
) -> AsyncIterator[_T | _HeartbeatSentinel]:
    """Await events from *event_iter*, yielding ``_HEARTBEAT`` while a single step is slow.

    Unlike ``asyncio.wait_for(event_iter.__anext__(), timeout=...)`` — which *cancels* the
    pending step on every timeout — the step runs in a shielded task so a slow GitHub call,
    a long tool batch (up to ``tool_timeout``) or a thinking-mode first token (30-120 s)
    keeps running while the caller emits SSE keepalives.  Cancelling the caller (client
    disconnect) cancels the pending step, preserving the previous cancellation semantics.
    """
    while True:
        # __anext__ 的类型是 Awaitable[_T]，create_task 需要 Coroutine —— cast 收紧
        step = asyncio.create_task(cast(Coroutine[Any, Any, _T], event_iter.__anext__()))
        try:
            while True:
                try:
                    event = await asyncio.wait_for(asyncio.shield(step), timeout=timeout)
                    break
                except TimeoutError:
                    yield _HEARTBEAT
                    continue
            yield event
        except StopAsyncIteration:
            break
        finally:
            # 请求取消/异常路径：确保未完成的一步被取消，不泄漏后台任务。
            if not step.done():
                step.cancel()
                with suppress(asyncio.CancelledError):
                    await step
