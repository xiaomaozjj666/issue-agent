"""In-process async task queue for batch issue analysis.

No Redis/Celery/arq — pure asyncio, matching the single-process deployment.
Uses a fixed set of asyncio workers for concurrency control and in-memory storage.

Lifecycle: the queue is started in the FastAPI lifespan and cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal

from openai import AsyncOpenAI

from app.agent import IssueAgent
from app.circuit_breaker import CircuitBreaker
from app.config import Settings
from app.github import GitHubClient, parse_issue_url
from app.models import AnalysisReport

logger = logging.getLogger(__name__)

TaskStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


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
    status: Literal["pending", "running", "completed", "partial"] = "pending"

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
    ) -> None:
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._max_concurrent = max_concurrent
        self._max_queue_size = max_queue_size
        self._max_history = max_history
        self._client = client
        self._github_client = github_client
        self._batches: dict[str, Batch] = {}
        self._pending: asyncio.Queue[tuple[str, str]] = asyncio.Queue()  # (batch_id, task_id)
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._running = False

    @property
    def queue_size(self) -> int:
        return self._pending.qsize()

    @property
    def batch_count(self) -> int:
        return len(self._batches)

    async def start(self) -> None:
        """Start the background worker."""
        if self._running:
            return
        self._running = True
        self._worker_tasks = [
            asyncio.create_task(self._worker(), name=f"issue-agent-batch-{index + 1}")
            for index in range(self._max_concurrent)
        ]
        logger.info("TaskQueue workers started (max_concurrent=%d)", self._max_concurrent)

    async def stop(self) -> None:
        """Cancel the background worker and mark pending tasks as cancelled."""
        self._running = False
        for worker in self._worker_tasks:
            worker.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()

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
                    progress = batch.progress
                    if progress["pending"] == 0 and progress["running"] == 0:
                        batch.status = "partial"
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
        """Retrieve a batch by ID."""
        return self._batches.get(batch_id)

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
            progress = batch.progress
            if progress["pending"] == 0 and progress["running"] == 0:
                batch.status = "completed" if progress["failed"] == 0 and progress["cancelled"] == 0 else "partial"


def _new_id() -> str:
    """Generate a short random ID (16 hex chars = 64 bit entropy)."""
    return secrets.token_hex(8)
