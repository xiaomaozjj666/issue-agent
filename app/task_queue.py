"""In-process async task queue for batch issue analysis.

No Redis/Celery/arq — pure asyncio, matching the single-process deployment.
Uses a fixed set of asyncio workers for concurrency control.

When a durable ``db_path`` is provided, batch/task state and completed reports
are written to SQLite so a process restart does not silently drop work:
- previously ``running`` tasks are marked ``cancelled`` (the worker died with them)
- ``pending`` tasks are re-enqueued and processed again
- ``completed`` tasks keep their report JSON for later ``GET /batch/{id}`` reads

Lifecycle: the queue is started in the FastAPI lifespan and cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Literal

import aiosqlite
from openai import AsyncOpenAI

from app.agent import IssueAgent
from app.circuit_breaker import CircuitBreaker
from app.config import Settings
from app.github import GitHubClient, parse_issue_url
from app.models import AnalysisReport

logger = logging.getLogger(__name__)

TaskStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
BatchStatus = Literal["pending", "running", "completed", "partial"]

# 持久化恢复时允许原样保留的状态：pending 会重新入队，其余终态直接展示
_RECOVERABLE_TASK_STATUSES = frozenset({"pending", "completed", "failed", "cancelled"})

_BATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id     TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batch_tasks (
    task_id      TEXT PRIMARY KEY,
    batch_id     TEXT NOT NULL,
    issue_url    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    error        TEXT,
    report_json  TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_batch_tasks_batch_id ON batch_tasks(batch_id);
CREATE INDEX IF NOT EXISTS idx_batch_tasks_status ON batch_tasks(status);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class BatchTask:
    """A single investigation task within a batch."""

    task_id: str
    issue_url: str
    status: TaskStatus = "pending"
    result: AnalysisReport | None = None
    error: str | None = None
    created_at: float = field(default_factory=monotonic)
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class Batch:
    """A collection of investigation tasks submitted together."""

    batch_id: str
    tasks: list[BatchTask]
    created_at: float = field(default_factory=monotonic)
    status: BatchStatus = "pending"

    @property
    def progress(self) -> dict:
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
        for task in self.tasks:
            counts[task.status] = counts.get(task.status, 0) + 1
        return counts


class TaskQueue:
    """Async task queue for batch issue investigations.

    Manages a bounded pool of concurrent investigations using fixed workers.
    Tasks are processed in FIFO order.
    """

    def __init__(
        self,
        settings: Settings,
        circuit_breaker: CircuitBreaker,
        *,
        max_concurrent: int = 2,
        max_queue_size: int = 100,
        max_history: int = 100,
        client: AsyncOpenAI | None = None,
        github_client: GitHubClient | None = None,
        db_path: str | None = None,
    ) -> None:
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._max_concurrent = max_concurrent
        self._max_queue_size = max_queue_size
        self._max_history = max_history
        self._client = client
        self._github_client = github_client
        self._db_path = db_path
        self._batches: dict[str, Batch] = {}
        self._pending: asyncio.Queue[tuple[str, str]] = asyncio.Queue()  # (batch_id, task_id)
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._running = False
        # SQLite 写操作串行化：submit 的异步持久化与 worker 落库可能并发
        self._store_lock = asyncio.Lock()
        # 强引用后台 create_task，防止任务在完成前被 GC
        self._persist_tasks: set[asyncio.Task[None]] = set()

    @property
    def queue_size(self) -> int:
        return self._pending.qsize()

    @property
    def batch_count(self) -> int:
        return len(self._batches)

    @property
    def durable(self) -> bool:
        return self._db_path is not None

    async def start(self) -> None:
        """Start the background worker and recover durable state if configured."""
        if self._running:
            return
        self._running = True
        self._worker_tasks = [
            asyncio.create_task(self._worker(), name=f"issue-agent-batch-{index + 1}")
            for index in range(self._max_concurrent)
        ]
        logger.info("TaskQueue workers started (max_concurrent=%d)", self._max_concurrent)
        if self._db_path is not None:
            try:
                recovered = await self.recover_from_store()
                if recovered:
                    logger.info("TaskQueue recovered %d pending task(s) from store", recovered)
            except Exception:
                logger.exception("TaskQueue recovery failed; continuing with empty in-memory queue")

    async def stop(self) -> None:
        """Cancel the background worker and mark pending tasks as cancelled."""
        self._running = False
        for worker in self._worker_tasks:
            worker.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()

        # 等待未完成的提交持久化，避免关机时丢写
        if self._persist_tasks:
            await asyncio.gather(*self._persist_tasks, return_exceptions=True)
            self._persist_tasks.clear()

        # Mark remaining pending tasks as cancelled
        while not self._pending.empty():
            try:
                batch_id, task_id = self._pending.get_nowait()
                batch = self._batches.get(batch_id)
                if batch is not None:
                    for task in batch.tasks:
                        if task.task_id == task_id and task.status == "pending":
                            task.status = "cancelled"
                            task.finished_at = monotonic()
                    batch.status = _recompute_batch_status(batch.progress)
                    for task in batch.tasks:
                        if task.task_id == task_id:
                            await self._persist_state(batch, task)
                            break
                self._pending.task_done()
            except asyncio.QueueEmpty:
                break

        logger.info("TaskQueue worker stopped")

    def submit(self, issue_urls: list[str]) -> Batch:
        """Submit a batch of issue URLs for investigation.

        Raises:
            ValueError: if the queue is full, issue_urls is empty, or any URL is invalid.
        """
        if not issue_urls:
            raise ValueError("At least one issue URL is required")
        # 提交时同步校验 URL：非法 URL 立即失败（HTTP 422），
        # 而不是等 worker 异步执行时才暴露为 failed。
        for url in issue_urls:
            parse_issue_url(url)
        if self._pending.qsize() + len(issue_urls) > self._max_queue_size:
            raise ValueError(
                f"Queue capacity exceeded: {self._pending.qsize()} pending + "
                f"{len(issue_urls)} new > {self._max_queue_size} max"
            )

        self._prune_completed_batches()
        batch_id = _new_id()
        tasks = [BatchTask(task_id=_new_id(), issue_url=url) for url in issue_urls]
        batch = Batch(batch_id=batch_id, tasks=tasks)
        self._batches[batch_id] = batch

        for task in tasks:
            self._pending.put_nowait((batch_id, task.task_id))

        logger.info("Batch %s submitted with %d tasks", batch_id, len(tasks))
        if self._db_path is not None:
            # 提交是同步 API，持久化放到下一跳，避免路由阻塞在磁盘 IO 上
            persist_task = asyncio.create_task(self._persist_submit(batch), name=f"batch-submit-{batch_id}")
            self._persist_tasks.add(persist_task)
            persist_task.add_done_callback(self._persist_tasks.discard)
        return batch

    def _prune_completed_batches(self) -> None:
        overflow = len(self._batches) - self._max_history + 1
        if overflow <= 0:
            return
        terminal = sorted(
            (batch for batch in self._batches.values() if batch.status in {"completed", "partial"}),
            key=lambda batch: batch.created_at,
        )
        for batch in terminal[:overflow]:
            self._batches.pop(batch.batch_id, None)

    def get_batch(self, batch_id: str) -> Batch | None:
        """In-memory lookup only. Use ``get_batch_async`` for durable fallback."""
        return self._batches.get(batch_id)

    async def get_batch_async(self, batch_id: str) -> Batch | None:
        """Retrieve a batch, falling back to SQLite when not resident in memory."""
        batch = self._batches.get(batch_id)
        if batch is not None:
            return batch
        if self._db_path is None:
            return None
        return await self._load_batch(batch_id)

    async def recover_from_store(self) -> int:
        """Reload pending work and cancel tasks orphaned by a previous process.

        Returns the number of tasks re-enqueued.
        """
        if self._db_path is None:
            return 0
        if not Path(self._db_path).exists():
            return 0
        async with self._store_lock:
            conn = await self._connect()
            try:
                await conn.execute(
                    "UPDATE batch_tasks SET status='cancelled', error=?, updated_at=? WHERE status='running'",
                    ("Worker process restarted before the task finished", _now()),
                )
                await conn.commit()
                rows = await (
                    await conn.execute(
                        "SELECT batch_id, task_id, issue_url, status, error, report_json "
                        "FROM batch_tasks WHERE status IN ('pending','completed','failed','cancelled') "
                        "ORDER BY created_at, task_id"
                    )
                ).fetchall()
            finally:
                await conn.close()

        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row["batch_id"], []).append(row)

        requeued = 0
        for batch_id, task_rows in grouped.items():
            tasks: list[BatchTask] = []
            for row in task_rows:
                result = None
                if row["report_json"]:
                    try:
                        result = AnalysisReport.model_validate_json(row["report_json"])
                    except Exception:
                        logger.warning("Failed to parse stored report for task %s", row["task_id"])
                task = BatchTask(
                    task_id=row["task_id"],
                    issue_url=row["issue_url"],
                    status=row["status"] if row["status"] in _RECOVERABLE_TASK_STATUSES else "cancelled",
                    result=result,
                    error=row["error"],
                )
                tasks.append(task)
            if not tasks:
                continue
            batch = Batch(batch_id=batch_id, tasks=tasks)
            batch.status = _recompute_batch_status(batch.progress)
            self._batches[batch_id] = batch
            # 回写批次状态，避免库中残留 crash 前的 running
            await self._persist_batch(batch)
            for task in tasks:
                if task.status == "pending":
                    self._pending.put_nowait((batch_id, task.task_id))
                    requeued += 1
        return requeued

    async def _connect(self) -> aiosqlite.Connection:
        if self._db_path is None:
            raise RuntimeError("TaskQueue has no durable store")
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path), timeout=5.0)
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            # 并发首连时 WAL 切换可能短暂锁库；busy_timeout 已兜底
            logger.debug("Could not set WAL journal mode", exc_info=True)
        await conn.execute("PRAGMA busy_timeout=5000")
        # 每次连接都保证表存在：并发首连时不能用进程内 flag 跳过建表
        await conn.executescript(_BATCH_SCHEMA)
        return conn

    async def _persist_submit(self, batch: Batch) -> None:
        try:
            async with self._store_lock:
                conn = await self._connect()
                try:
                    await conn.execute(
                        "INSERT OR IGNORE INTO batches (batch_id, status, created_at, updated_at) VALUES (?,?,?,?)",
                        (batch.batch_id, batch.status, _now(), _now()),
                    )
                    for task in batch.tasks:
                        await conn.execute(
                            "INSERT OR IGNORE INTO batch_tasks "
                            "(task_id, batch_id, issue_url, status, error, report_json, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (
                                task.task_id,
                                batch.batch_id,
                                task.issue_url,
                                task.status,
                                task.error,
                                None,
                                _now(),
                                _now(),
                            ),
                        )
                    await conn.commit()
                finally:
                    await conn.close()
        except Exception:
            logger.exception("Failed to persist batch %s submit", batch.batch_id)

    async def _persist_state(self, batch: Batch, task: BatchTask | None = None) -> None:
        """Write task and/or batch status in one transaction."""
        if self._db_path is None:
            return
        report_json = task.result.model_dump_json() if task is not None and task.result is not None else None
        try:
            async with self._store_lock:
                conn = await self._connect()
                try:
                    if task is not None:
                        await conn.execute(
                            "INSERT OR REPLACE INTO batch_tasks "
                            "(task_id, batch_id, issue_url, status, error, report_json, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,COALESCE((SELECT created_at FROM batch_tasks WHERE task_id=?), ?), ?)",
                            (
                                task.task_id,
                                batch.batch_id,
                                task.issue_url,
                                task.status,
                                task.error,
                                report_json,
                                task.task_id,
                                _now(),
                                _now(),
                            ),
                        )
                    await conn.execute(
                        "INSERT OR REPLACE INTO batches (batch_id, status, created_at, updated_at) VALUES (?,?,?,?)",
                        (batch.batch_id, batch.status, _now(), _now()),
                    )
                    await conn.commit()
                finally:
                    await conn.close()
        except Exception:
            logger.exception("Failed to persist batch %s state", batch.batch_id)

    async def _persist_batch(self, batch: Batch) -> None:
        await self._persist_state(batch, None)

    async def _load_batch(self, batch_id: str) -> Batch | None:
        conn = await self._connect()
        try:
            batch_row = await (
                await conn.execute("SELECT batch_id, status FROM batches WHERE batch_id=?", (batch_id,))
            ).fetchone()
            if batch_row is None:
                return None
            task_rows = await (
                await conn.execute(
                    "SELECT task_id, issue_url, status, error, report_json FROM batch_tasks "
                    "WHERE batch_id=? ORDER BY created_at, task_id",
                    (batch_id,),
                )
            ).fetchall()
        finally:
            await conn.close()
        tasks: list[BatchTask] = []
        for row in task_rows:
            result = None
            if row["report_json"]:
                try:
                    result = AnalysisReport.model_validate_json(row["report_json"])
                except Exception:
                    logger.warning("Failed to parse stored report for task %s", row["task_id"])
            status = row["status"] if row["status"] in _RECOVERABLE_TASK_STATUSES else "cancelled"
            tasks.append(
                BatchTask(
                    task_id=row["task_id"],
                    issue_url=row["issue_url"],
                    status=status,  # type: ignore[arg-type]
                    result=result,
                    error=row["error"],
                )
            )
        if not tasks:
            return None
        batch = Batch(batch_id=batch_id, tasks=tasks)
        # 以任务状态重算批次状态，避免读到 crash 前写下的 running
        batch.status = _recompute_batch_status(batch.progress)
        return batch

    async def _worker(self) -> None:
        """Background worker: continuously dequeue and execute tasks."""
        while True:
            batch_id, task_id = await self._pending.get()
            try:
                await self._run_task(batch_id, task_id)
            finally:
                self._pending.task_done()

    async def _run_task(self, batch_id: str, task_id: str) -> None:
        batch = self._batches.get(batch_id)
        task = next((item for item in batch.tasks if item.task_id == task_id), None) if batch else None
        if task is None or batch is None:
            logger.warning("Task %s/%s not found, skipping", batch_id, task_id)
            return

        task.status = "running"
        task.started_at = monotonic()
        if batch.status == "pending":
            batch.status = "running"
        await self._persist_state(batch, task)

        agent = IssueAgent(
            self._settings,
            client=self._client,
            github_client=self._github_client.fork() if self._github_client is not None else None,
            circuit_breaker=self._circuit_breaker,
        )
        try:
            task.result = await agent.investigate(task.issue_url)
            task.status = "completed"
        except asyncio.CancelledError:
            task.status = "cancelled"
            raise
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)[:500]
            logger.exception("Batch task %s failed: %s", task.task_id, task.issue_url)
        finally:
            # aclose 可能抛 CancelledError（事件循环关闭期被取消），
            # 用 try/except 包裹避免掩盖原异常；batch.status 计算必须执行
            try:
                await agent.aclose()
            except Exception:
                logger.exception("agent.aclose() failed during task cleanup")
            task.finished_at = monotonic()
            batch.status = _recompute_batch_status(batch.progress)
            await self._persist_state(batch, task)


def _recompute_batch_status(progress: dict) -> BatchStatus:
    if progress["pending"] or progress["running"]:
        return "pending" if progress["running"] == 0 else "running"
    if progress["failed"] or progress["cancelled"]:
        return "partial"
    return "completed"


def _new_id() -> str:
    """Generate a short random ID (16 hex chars = 64 bit entropy)."""
    return secrets.token_hex(8)
