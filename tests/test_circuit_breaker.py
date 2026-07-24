"""Unit tests for the circuit breaker state machine."""

import asyncio
import contextlib

import pytest

from app.circuit_breaker import CircuitBreaker, State
from app.errors import CircuitBreakerOpenError


class TestCircuitBreaker:
    """Test the CircuitBreaker state machine transitions."""

    def test_initial_state_closed(self):
        breaker = CircuitBreaker(threshold=3, recovery=30.0)
        assert breaker.state == State.CLOSED
        assert breaker.failure_count == 0

    def test_closed_stays_closed_on_success(self):
        breaker = CircuitBreaker(threshold=3, recovery=30.0)

        async def run():
            return await breaker.call(_ok)

        asyncio.run(run())
        assert breaker.state == State.CLOSED
        assert breaker.failure_count == 0

    def test_closed_stays_closed_below_threshold(self):
        breaker = CircuitBreaker(threshold=3, recovery=30.0)

        async def run():
            for _ in range(2):
                with contextlib.suppress(RuntimeError):
                    await breaker.call(_fail)

        asyncio.run(run())
        assert breaker.state == State.CLOSED
        assert breaker.failure_count == 2

    def test_closed_opens_at_threshold(self):
        breaker = CircuitBreaker(threshold=3, recovery=30.0)

        async def run():
            for _ in range(3):
                with contextlib.suppress(RuntimeError):
                    await breaker.call(_fail)

        asyncio.run(run())
        assert breaker.state == State.OPEN
        assert breaker.failure_count == 3

    def test_open_fast_fails(self):
        breaker = CircuitBreaker(threshold=2, recovery=30.0)

        async def run():
            for _ in range(2):
                with contextlib.suppress(RuntimeError):
                    await breaker.call(_fail)
            # Now OPEN
            with pytest.raises(CircuitBreakerOpenError):
                await breaker.call(_ok)

        asyncio.run(run())

    def test_half_open_probe_succeeds(self):
        """After recovery timeout, circuit transitions to half-open and a successful probe resets it."""
        breaker = CircuitBreaker(threshold=2, recovery=0.001)  # minimal recovery for immediate half-open

        async def run():
            for _ in range(2):
                with contextlib.suppress(RuntimeError):
                    await breaker.call(_fail)
            # recovery=1ms means it goes half-open immediately
            await asyncio.sleep(0.01)
            result = await breaker.call(_ok)
            assert result == "ok"
            assert breaker.state == State.CLOSED
            assert breaker.failure_count == 0

        asyncio.run(run())

    def test_half_open_probe_fails(self):
        breaker = CircuitBreaker(threshold=2, recovery=0.001)

        async def run():
            for _ in range(2):
                with contextlib.suppress(RuntimeError):
                    await breaker.call(_fail)
            await asyncio.sleep(0.01)
            # Probe fails → back to OPEN
            with contextlib.suppress(RuntimeError):
                await breaker.call(_fail)
            assert breaker.state == State.OPEN

        asyncio.run(run())

    def test_reset(self):
        breaker = CircuitBreaker(threshold=2, recovery=30.0)

        async def run():
            for _ in range(2):
                with contextlib.suppress(RuntimeError):
                    await breaker.call(_fail)
            assert breaker.state == State.OPEN
            breaker.reset()
            assert breaker.state == State.CLOSED
            assert breaker.failure_count == 0

        asyncio.run(run())

    def test_success_resets_counter(self):
        breaker = CircuitBreaker(threshold=5, recovery=30.0)

        async def run():
            # 2 failures, then success → counter resets
            for _ in range(2):
                with contextlib.suppress(RuntimeError):
                    await breaker.call(_fail)
            await breaker.call(_ok)
            assert breaker.failure_count == 0

        asyncio.run(run())

    def test_concurrent_calls_serialize_state(self):
        """Concurrent calls should not race on state transitions."""
        breaker = CircuitBreaker(threshold=3, recovery=30.0)

        async def run():
            async def fail_once():
                with contextlib.suppress(RuntimeError):
                    await breaker.call(_fail)

            await asyncio.gather(*[fail_once() for _ in range(3)])
            # After 3 concurrent failures, circuit should be OPEN
            assert breaker.state == State.OPEN

        asyncio.run(run())

    def test_invalid_threshold(self):
        with pytest.raises(ValueError):
            CircuitBreaker(threshold=0, recovery=1.0)

    def test_invalid_recovery(self):
        with pytest.raises(ValueError):
            CircuitBreaker(threshold=1, recovery=0)


# ── test helpers ────────────────────────────────────────────────


async def _ok():
    return "ok"


async def _fail():
    raise RuntimeError("simulated failure")
