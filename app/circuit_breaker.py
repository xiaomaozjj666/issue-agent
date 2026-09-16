"""In-process circuit breaker for LLM provider calls.

Implements the standard CLOSED → OPEN → HALF_OPEN state machine with
consecutive failure counting. When the circuit opens, subsequent calls
fast-fail with ``CircuitBreakerOpenError`` instead of waiting for the
full ``openai_timeout``, preserving worker capacity.

No external dependencies — pure asyncio.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto
from time import monotonic
from typing import Any, TypeVar

from app.errors import CircuitBreakerOpenError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 只有这些状态码代表 provider 侧不健康；其余 4xx 属于调用方输入问题。
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def is_retryable_failure(exc: BaseException) -> bool:
    """Whether *exc* indicates provider ill-health (and should count toward the breaker).

    Client-side input errors (400/401/403/404/422 …) must NOT open the circuit: a bad
    model name or a malformed request is the caller's problem, and counting those lets
    five bad requests take the provider offline for every other user.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in _RETRYABLE_STATUS or status >= 500
    return True  # 连接/超时/协议错误等没有 status_code，一律视为可重试


class State(Enum):
    CLOSED = auto()  # Normal operation
    OPEN = auto()  # Fast-fail all requests
    HALF_OPEN = auto()  # Allow one probe


@dataclass
class CircuitBreaker:
    """Async circuit breaker wrapping an LLM API callable.

    Usage::

        breaker = CircuitBreaker(threshold=5, recovery=30.0)
        result = await breaker.call(client.chat.completions.create, **kwargs)
    """

    threshold: int = 5
    recovery: float = 30.0  # seconds before half-open probe

    _state: State = State.CLOSED
    _failure_count: int = 0
    _last_failure_time: float = 0.0
    _lock: asyncio.Lock | None = None
    _probe_in_flight: bool = False  # HALF_OPEN 期间只允许一个探针

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("threshold must be >= 1")
        if self.recovery <= 0:
            raise ValueError("recovery must be > 0")

    @property
    def state(self) -> State:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def call(
        self,
        fn: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute *fn* under circuit breaker protection.

        Raises:
            CircuitBreakerOpenError: if the circuit is OPEN.
            The original exception: if *fn* raises and the circuit remains CLOSED.
        """
        async with self._get_lock():
            if self._state == State.OPEN:
                if monotonic() - self._last_failure_time >= self.recovery:
                    self._state = State.HALF_OPEN
                    self._probe_in_flight = True
                    logger.info("Circuit breaker: OPEN → HALF_OPEN (probing)")
                else:
                    remaining = self.recovery - (monotonic() - self._last_failure_time)
                    raise CircuitBreakerOpenError(
                        f"LLM provider circuit is open. "
                        f"Retry in {remaining:.0f}s "
                        f"(failures: {self._failure_count}/{self.threshold})"
                    )
            elif self._state == State.HALF_OPEN and self._probe_in_flight:
                # 已有探针在执行，其他请求继续 fast-fail
                raise CircuitBreakerOpenError(
                    "LLM provider circuit is half-open; waiting for probe result"
                )

        # Execute the call *outside* the lock so concurrent requests
        # don't serialize on the API call itself.
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            if is_retryable_failure(exc):
                await self._record_failure(exc)
            else:
                # 请求级错误（400/401/403/404/422 等）是调用方的问题，不是 provider 故障：
                # 若计入失败，5 次写错的 model 名就能把整个 provider 熔断 30 秒。
                logger.info("Circuit breaker: ignoring non-retryable failure (%s)", exc)
            raise

        await self._record_success()
        return result

    async def report_stream_outcome(self, exc: Exception | None = None) -> None:
        """Record the outcome of a *streamed* provider call once the stream is consumed.

        ``call()`` records success as soon as the streaming response object comes back —
        before a single token arrives — so mid-stream disconnects were invisible to the
        breaker (and even reset its failure count). Streaming callers report the real
        outcome here instead.
        """
        if exc is None:
            await self._record_success()
        elif is_retryable_failure(exc):
            await self._record_failure(exc)

    async def _record_failure(self, exc: Exception) -> None:
        async with self._get_lock():
            self._failure_count += 1
            self._last_failure_time = monotonic()
            if self._state == State.HALF_OPEN:
                self._state = State.OPEN
                self._probe_in_flight = False
                logger.warning(
                    "Circuit breaker: HALF_OPEN probe failed → OPEN (%d/%d failures: %s)",
                    self._failure_count,
                    self.threshold,
                    exc,
                )
            elif self._failure_count >= self.threshold:
                self._state = State.OPEN
                logger.error(
                    "Circuit breaker: CLOSED → OPEN (%d/%d failures: %s)",
                    self._failure_count,
                    self.threshold,
                    exc,
                )

    async def _record_success(self) -> None:
        async with self._get_lock():
            if self._state == State.HALF_OPEN:
                logger.info("Circuit breaker: HALF_OPEN probe succeeded → CLOSED")
            self._state = State.CLOSED
            self._failure_count = 0
            self._probe_in_flight = False

    def reset(self) -> None:
        """Force the circuit back to CLOSED (e.g. for testing or manual intervention)."""
        self._state = State.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._probe_in_flight = False
