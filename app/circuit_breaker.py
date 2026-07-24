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
                    logger.info("Circuit breaker: OPEN → HALF_OPEN (probing)")
                else:
                    remaining = self.recovery - (monotonic() - self._last_failure_time)
                    raise CircuitBreakerOpenError(
                        f"LLM provider circuit is open. "
                        f"Retry in {remaining:.0f}s "
                        f"(failures: {self._failure_count}/{self.threshold})"
                    )

        # Execute the call *outside* the lock so concurrent requests
        # don't serialize on the API call itself.
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            await self._record_failure(exc)
            raise

        await self._record_success()
        return result

    async def _record_failure(self, exc: Exception) -> None:
        async with self._get_lock():
            self._failure_count += 1
            self._last_failure_time = monotonic()
            if self._state == State.HALF_OPEN:
                self._state = State.OPEN
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

    def reset(self) -> None:
        """Force the circuit back to CLOSED (e.g. for testing or manual intervention)."""
        self._state = State.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
