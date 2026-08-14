"""``_iter_events_with_heartbeat`` 回归测试（15s SSE 心跳取消 bug）。

背景：旧实现用 ``asyncio.wait_for(event_iter.__anext__(), timeout=15.0)`` 做 SSE 心跳，
超时会**取消**正在执行的生成器步骤——任何超过 15 秒无事件的操作（慢 GitHub API 调用、
长工具批次、thinking 模式报告阶段首 token）都会让生成器终结，流随后以
``StopAsyncIteration`` 结束，会话被标记为 completed 但**没有报告**。

新实现把每个步骤放进 shield 任务：超时只输出心跳，绝不取消进行中的步骤；
只有请求本身被取消时才取消步骤（保持原有取消语义）。
"""

import asyncio

import pytest

from app.main import _HEARTBEAT, _iter_events_with_heartbeat


async def _stepping_generator(steps: int, step_sleep: float):
    """每步先 sleep 再产出一个值，模拟慢工具调用 / 模型首 token 等待。"""
    for index in range(steps):
        await asyncio.sleep(step_sleep)
        yield f"event-{index}"


async def test_slow_step_emits_heartbeats_and_completes() -> None:
    """慢步骤：心跳持续输出，步骤不被取消，事件最终完整送达。"""
    collected: list = []
    async for item in _iter_events_with_heartbeat(_stepping_generator(1, 0.15), timeout=0.03):
        collected.append(item)
    # 事件必须到达（旧实现下这里会直接 StopAsyncIteration，事件丢失）
    assert collected[-1] == "event-0"
    # 等待期间输出过至少一个心跳
    assert _HEARTBEAT in collected
    assert collected.count(_HEARTBEAT) >= 3


async def test_fast_steps_pass_through_without_heartbeat() -> None:
    """快速步骤：无心跳，事件按序透传。"""
    collected: list = []
    async for item in _iter_events_with_heartbeat(_stepping_generator(2, 0.001), timeout=0.1):
        collected.append(item)
    assert collected == ["event-0", "event-1"]


async def test_empty_generator_ends_iteration() -> None:
    """生成器立即结束：迭代正常终止，无心跳。"""
    collected: list = []
    async for item in _iter_events_with_heartbeat(_stepping_generator(0, 0), timeout=0.01):
        collected.append(item)
    assert collected == []


async def test_consumer_cancellation_cancels_pending_step() -> None:
    """请求被取消（客户端断开）时，进行中的步骤必须被取消，不泄漏后台任务。"""
    step_cancelled = asyncio.Event()

    async def tracker_generator():
        try:
            await asyncio.sleep(10)
            yield "never"
        except asyncio.CancelledError:
            step_cancelled.set()
            raise

    async def consume() -> None:
        async for _item in _iter_events_with_heartbeat(tracker_generator(), timeout=0.02):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # 让消费者进入慢步骤
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert step_cancelled.is_set(), "取消消费者后，进行中的生成器步骤应被取消"


async def test_slow_multistep_generator_is_not_truncated() -> None:
    """多步慢生成器：每一步都完整执行（旧实现会在第一步超时后截断）。"""
    collected: list = []
    async for item in _iter_events_with_heartbeat(_stepping_generator(3, 0.06), timeout=0.02):
        collected.append(item)
    # 过滤心跳后，三个事件必须完整按序到达
    events_only = [item for item in collected if item is not _HEARTBEAT]
    assert events_only == ["event-0", "event-1", "event-2"]
