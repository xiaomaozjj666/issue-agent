"""Unit tests for the background task queue."""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.task_queue import Batch, BatchTask, TaskQueue


class TestBatchTask:
    def test_task_initial_state(self):
        task = BatchTask(task_id="abc123", issue_url="https://github.com/foo/bar/issues/1")
        assert task.status == "pending"
        assert task.result is None
        assert task.error is None


class TestBatch:
    def test_batch_progress_empty(self):
        batch = Batch(batch_id="batch1", tasks=[])
        assert batch.progress == {"pending": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}

    def test_batch_progress_mixed(self):
        tasks = [
            BatchTask(task_id="1", issue_url="https://github.com/foo/bar/issues/1", status="completed"),
            BatchTask(task_id="2", issue_url="https://github.com/foo/bar/issues/2", status="running"),
            BatchTask(task_id="3", issue_url="https://github.com/foo/bar/issues/3", status="pending"),
            BatchTask(task_id="4", issue_url="url4", status="failed"),
        ]
        batch = Batch(batch_id="batch1", tasks=tasks)
        progress = batch.progress
        assert progress["completed"] == 1
        assert progress["running"] == 1
        assert progress["pending"] == 1
        assert progress["failed"] == 1


class TestTaskQueue:
    """Test the TaskQueue lifecycle and operations."""

    @pytest.fixture
    def settings(self):
        s = MagicMock()
        s.circuit_breaker_threshold = 5
        s.circuit_breaker_recovery = 30.0
        s.batch_max_concurrent = 2
        s.batch_max_queue_size = 10
        return s

    @pytest.fixture
    def breaker(self):
        b = MagicMock()
        return b

    @pytest.fixture
    def queue(self, settings, breaker):
        q = TaskQueue(settings, breaker, max_concurrent=2, max_queue_size=10)
        return q

    def test_submit_creates_batch(self, queue):
        batch = queue.submit(["https://github.com/foo/bar/issues/1", "https://github.com/foo/bar/issues/2"])
        assert len(batch.tasks) == 2
        assert batch.status == "pending"
        assert all(t.status == "pending" for t in batch.tasks)

    def test_submit_empty_raises(self, queue):
        with pytest.raises(ValueError, match="At least one issue URL"):
            queue.submit([])

    def test_submit_rejects_invalid_url_immediately(self, queue):
        """非法 URL 应在提交时同步拒绝（HTTP 422），而不是等 worker 异步失败。"""
        with pytest.raises(ValueError, match="issue_url must"):
            queue.submit(["not-a-url"])
        with pytest.raises(ValueError, match="issue_url must"):
            queue.submit(["https://github.com/acme/widget/pull/42"])
        # 合法 URL 正常入队
        batch = queue.submit(["https://github.com/acme/widget/issues/1"])
        assert len(batch.tasks) == 1

    def test_get_batch(self, queue):
        batch = queue.submit(["https://github.com/foo/bar/issues/1"])
        retrieved = queue.get_batch(batch.batch_id)
        assert retrieved is not None
        assert retrieved.batch_id == batch.batch_id

    def test_get_batch_not_found(self, queue):
        assert queue.get_batch("nonexistent") is None

    def test_submit_exceeds_capacity(self, queue):
        queue._max_queue_size = 2
        queue._pending.put_nowait(("b1", "t1"))
        queue._pending.put_nowait(("b1", "t2"))
        with pytest.raises(ValueError, match="Queue capacity exceeded"):
            queue.submit(["https://github.com/foo/bar/issues/1"])

    def test_start_stop_lifecycle(self, settings, breaker):
        queue = TaskQueue(settings, breaker)
        assert not queue._running

        async def run():
            await queue.start()
            assert queue._running
            await queue.stop()
            assert not queue._running

        asyncio.run(run())

    def test_stop_cancels_pending(self, settings, breaker):
        queue = TaskQueue(settings, breaker, max_concurrent=1, max_queue_size=10)
        batch = queue.submit(["https://github.com/foo/bar/issues/1", "https://github.com/foo/bar/issues/2"])
        assert queue._pending.qsize() == 2

        async def run():
            await queue.start()
            # Let the worker pick up for a moment
            await asyncio.sleep(0.1)
            await queue.stop()
            # Remaining tasks should be marked cancelled
            for task in batch.tasks:
                assert task.status in ("completed", "failed", "cancelled")

        asyncio.run(run())

    def test_queue_size(self, queue):
        assert queue.queue_size == 0
        queue.submit(["https://github.com/foo/bar/issues/1", "https://github.com/foo/bar/issues/2"])
        assert queue.queue_size == 2

    def test_batch_count(self, queue):
        assert queue.batch_count == 0
        queue.submit(["https://github.com/foo/bar/issues/1"])
        assert queue.batch_count == 1
        queue.submit(["https://github.com/foo/bar/issues/2"])
        assert queue.batch_count == 2

    def test_submit_prunes_oldest_terminal_batch_history(self, settings, breaker):
        queue = TaskQueue(settings, breaker, max_queue_size=10, max_history=2)
        oldest = queue.submit(["https://github.com/foo/bar/issues/1"])
        oldest.status = "completed"
        middle = queue.submit(["https://github.com/foo/bar/issues/2"])
        middle.status = "partial"

        newest = queue.submit(["https://github.com/foo/bar/issues/3"])

        assert queue.get_batch(oldest.batch_id) is None
        assert queue.get_batch(middle.batch_id) is middle
        assert queue.get_batch(newest.batch_id) is newest
        assert queue.batch_count == 2

    async def test_workers_honor_configured_concurrency(self, settings, breaker, monkeypatch):
        active = 0
        peak_active = 0
        two_started = asyncio.Event()
        release = asyncio.Event()

        class FakeAgent:
            def __init__(self, *_args, **_kwargs):
                pass

            async def investigate(self, _issue_url):
                nonlocal active, peak_active
                active += 1
                peak_active = max(peak_active, active)
                if active == 2:
                    two_started.set()
                try:
                    await release.wait()
                    return MagicMock()
                finally:
                    active -= 1

            async def aclose(self):
                pass

        monkeypatch.setattr("app.task_queue.IssueAgent", FakeAgent)
        queue = TaskQueue(settings, breaker, max_concurrent=2, max_queue_size=10)
        batch = queue.submit(["https://github.com/foo/bar/issues/1", "https://github.com/foo/bar/issues/2", "https://github.com/foo/bar/issues/3"])

        await queue.start()
        try:
            await asyncio.wait_for(two_started.wait(), timeout=1)
            assert peak_active == 2
            assert batch.progress["running"] == 2
            release.set()
            await asyncio.wait_for(queue._pending.join(), timeout=1)
            assert batch.status == "completed"
            assert batch.progress["completed"] == 3
        finally:
            release.set()
            await queue.stop()

    async def test_stop_marks_running_tasks_cancelled(self, settings, breaker, monkeypatch):
        started = asyncio.Event()

        class BlockingAgent:
            def __init__(self, *_args, **_kwargs):
                pass

            async def investigate(self, _issue_url):
                started.set()
                await asyncio.Event().wait()

            async def aclose(self):
                pass

        monkeypatch.setattr("app.task_queue.IssueAgent", BlockingAgent)
        queue = TaskQueue(settings, breaker, max_concurrent=1, max_queue_size=10)
        batch = queue.submit(["https://github.com/foo/bar/issues/1"])

        await queue.start()
        await asyncio.wait_for(started.wait(), timeout=1)
        await queue.stop()

        assert batch.tasks[0].status == "cancelled"
        assert batch.status == "partial"
