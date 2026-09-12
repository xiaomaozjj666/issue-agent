"""Batch queue durable-store tests (SQLite file, no network)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.circuit_breaker import CircuitBreaker
from app.config import Settings
from app.task_queue import TaskQueue


@pytest.fixture
def durable_queue(tmp_path: Path):
    settings = Settings(openai_api_key="test-key", independent_review=False)
    db_path = str(tmp_path / "batches.db")
    queue = TaskQueue(
        settings,
        CircuitBreaker(threshold=5, recovery=1.0),
        max_concurrent=1,
        max_queue_size=10,
        max_history=10,
        db_path=db_path,
    )
    return queue, db_path


async def _seed_store(db_path: str) -> None:
    import aiosqlite

    conn = await aiosqlite.connect(db_path)
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batches (
            batch_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_tasks (
            task_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            issue_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            report_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    await conn.execute(
        "INSERT INTO batches (batch_id, status) VALUES (?, ?)",
        ("batch-old", "running"),
    )
    await conn.execute(
        "INSERT INTO batch_tasks (task_id, batch_id, issue_url, status) VALUES (?,?,?,?)",
        ("task-run", "batch-old", "https://github.com/a/b/issues/1", "running"),
    )
    await conn.execute(
        "INSERT INTO batch_tasks (task_id, batch_id, issue_url, status) VALUES (?,?,?,?)",
        ("task-pend", "batch-old", "https://github.com/a/b/issues/2", "pending"),
    )
    await conn.commit()
    await conn.close()


async def test_recover_cancels_running_and_requeues_pending(durable_queue) -> None:
    queue, db_path = durable_queue
    await _seed_store(db_path)
    # 不启动 worker，只测恢复逻辑
    requeued = await queue.recover_from_store()
    assert requeued == 1
    batch = await queue.get_batch_async("batch-old")
    assert batch is not None
    by_id = {t.task_id: t for t in batch.tasks}
    assert by_id["task-run"].status == "cancelled"
    assert by_id["task-pend"].status == "pending"


async def test_submit_persists_batch_tasks(durable_queue) -> None:
    queue, _db = durable_queue
    # 不 start worker：持久化路径不需要真正执行调查，避免测试触网
    batch = queue.submit(["https://github.com/a/b/issues/9"])
    # submit 的持久化是 create_task，让事件循环跑一拍
    await asyncio.sleep(0.05)
    loaded = await queue.get_batch_async(batch.batch_id)
    assert loaded is not None
    assert loaded.tasks[0].status == "pending"


async def test_without_db_path_is_memory_only() -> None:
    settings = Settings(openai_api_key="test-key")
    queue = TaskQueue(settings, CircuitBreaker(threshold=5, recovery=1.0), db_path=None)
    assert queue.durable is False
    assert await queue.recover_from_store() == 0
    batch = queue.submit(["https://github.com/a/b/issues/3"])
    assert queue.get_batch(batch.batch_id) is batch
    assert await queue.get_batch_async(batch.batch_id) is batch


async def test_run_task_persists_completed_report(monkeypatch, durable_queue) -> None:
    from app import task_queue as tq
    from app.models import AnalysisReport

    queue, db_path = durable_queue
    report = AnalysisReport(
        summary="s",
        root_cause="r",
        confidence="high",
        evidence=[],
        proposed_changes=[],
        tests=[],
        risks=[],
    )

    class _Agent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def investigate(self, url: str):
            return report

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(tq, "IssueAgent", _Agent)
    await queue.start()
    try:
        batch = queue.submit(["https://github.com/a/b/issues/4"])
        # 等内存完成，再等一拍让 finally 中的落库结束
        for _ in range(80):
            current = queue.get_batch(batch.batch_id)
            if current and current.tasks[0].status == "completed":
                break
            await asyncio.sleep(0.02)
        assert queue.get_batch(batch.batch_id).tasks[0].status == "completed"
        await asyncio.sleep(0.05)

        # 经队列自己的连接读库（避免测试侧再开连接与写锁打架）
        queue._batches.clear()
        loaded = await queue.get_batch_async(batch.batch_id)
        assert loaded is not None
        assert loaded.tasks[0].status == "completed"
        assert loaded.tasks[0].result is not None
        assert loaded.tasks[0].result.root_cause == "r"
    finally:
        await queue.stop()


async def test_run_task_persists_failure(durable_queue, monkeypatch) -> None:
    from app import task_queue as tq

    queue, _db = durable_queue

    class _Agent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def investigate(self, url: str):
            raise RuntimeError("boom")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(tq, "IssueAgent", _Agent)
    await queue.start()
    try:
        batch = queue.submit(["https://github.com/a/b/issues/5"])
        for _ in range(50):
            current = queue.get_batch(batch.batch_id)
            if current and current.tasks[0].status == "failed":
                break
            await asyncio.sleep(0.02)
        task = queue.get_batch(batch.batch_id).tasks[0]
        assert task.status == "failed"
        assert "boom" in (task.error or "")
    finally:
        await queue.stop()


async def test_load_batch_skips_invalid_report_json(durable_queue) -> None:
    import aiosqlite

    queue, db_path = durable_queue
    conn = await aiosqlite.connect(db_path)
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batches (
            batch_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            created_at TEXT, updated_at TEXT)
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_tasks (
            task_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, issue_url TEXT NOT NULL,
            status TEXT NOT NULL, error TEXT, report_json TEXT,
            created_at TEXT, updated_at TEXT)
        """
    )
    await conn.execute("INSERT INTO batches (batch_id, status) VALUES ('b-bad', 'completed')")
    await conn.execute(
        "INSERT INTO batch_tasks (task_id, batch_id, issue_url, status, report_json) VALUES "
        "('t-bad', 'b-bad', 'https://github.com/a/b/issues/6', 'completed', '{not-json')",
    )
    await conn.execute(
        "INSERT INTO batch_tasks (task_id, batch_id, issue_url, status) VALUES "
        "('t-ok', 'b-bad', 'https://github.com/a/b/issues/7', 'running')",
    )
    await conn.commit()
    await conn.close()

    loaded = await queue.get_batch_async("b-bad")
    assert loaded is not None
    by_id = {t.task_id: t for t in loaded.tasks}
    assert by_id["t-bad"].result is None
    assert by_id["t-ok"].status == "cancelled"


async def test_recompute_batch_status_helpers() -> None:
    from app.task_queue import _recompute_batch_status

    def _p(**kwargs):
        base = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
        base.update(kwargs)
        return _recompute_batch_status(base)

    assert _p(pending=1) == "pending"
    assert _p(running=1) == "running"
    assert _p(completed=2) == "completed"
    assert _p(completed=1, failed=1) == "partial"


async def test_start_recovers_store_without_executing_network(durable_queue, monkeypatch) -> None:
    from app import task_queue as tq

    queue, db_path = durable_queue
    await _seed_store(db_path)

    class _Agent:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def investigate(self, url: str):
            raise RuntimeError("should not investigate in this test")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(tq, "IssueAgent", _Agent)
    await queue.start()
    try:
        batch = await queue.get_batch_async("batch-old")
        assert batch is not None
        statuses = {t.task_id: t.status for t in batch.tasks}
        assert statuses["task-run"] == "cancelled"
    finally:
        await queue.stop()
