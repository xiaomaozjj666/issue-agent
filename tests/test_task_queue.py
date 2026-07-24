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
            BatchTask(task_id="1", issue_url="url1", status="completed"),
            BatchTask(task_id="2", issue_url="url2", status="running"),
            BatchTask(task_id="3", issue_url="url3", status="pending"),
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
        batch = queue.submit(["url1", "url2"])
        assert len(batch.tasks) == 2
        assert batch.status == "pending"
        assert all(t.status == "pending" for t in batch.tasks)

    def test_submit_empty_raises(self, queue):
        with pytest.raises(ValueError, match="At least one issue URL"):
            queue.submit([])

    def test_get_batch(self, queue):
        batch = queue.submit(["url1"])
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
            queue.submit(["url1"])

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
        batch = queue.submit(["url1", "url2"])
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
        queue.submit(["url1", "url2"])
        assert queue.queue_size == 2

    def test_batch_count(self, queue):
        assert queue.batch_count == 0
        queue.submit(["url1"])
        assert queue.batch_count == 1
        queue.submit(["url2"])
        assert queue.batch_count == 2
