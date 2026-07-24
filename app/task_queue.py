"""In-process async task queue for batch issue analysis.

No Redis/Celery/arq — pure asyncio, matching the single-process deployment.
Uses ``asyncio.Semaphore`` for concurrency control and in-memory storage.

Lifecycle: the queue is started in the FastAPI lifespan and cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal

from app.agent import IssueAgent
from app.circuit_breaker import CircuitBreaker
from app.config import Settings
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

    Manages a bounded pool of concurrent investigations using a semaphore.
    Tasks are processed in FIFO order.
    """

    def __init__(
        self,
        settings: Settings,
        circuit_breaker: CircuitBreaker,
        *,
        max_concurrent: int = 2,
        max_queue_size: int = 100,
    ) -> None:
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_queue_size = max_queue_size
        self._batches: dict[str, Batch] = {}
        self._pending: asyncio.Queue[tuple[str, str]] = asyncio.Queue()  # (batch_id, task_id)
        self._worker_task: asyncio.Task[None] | None = None
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
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("TaskQueue worker started (max_concurrent=%d)", self._semaphore._value)

    async def stop(self) -> None:
        """Cancel the background worker and mark pending tasks as cancelled."""
        self._running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None

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
            except asyncio.QueueEmpty:
                break

        logger.info("TaskQueue worker stopped")

    def submit(self, issue_urls: list[str]) -> Batch:
        """Submit a batch of issue URLs for investigation.

        Raises:
            ValueError: if the queue is full or issue_urls is empty.
        """
        if not issue_urls:
            raise ValueError("At least one issue URL is required")
        if self._pending.qsize() + len(issue_urls) > self._max_queue_size:
            raise ValueError(
                f"Queue capacity exceeded: {self._pending.qsize()} pending + "
                f"{len(issue_urls)} new > {self._max_queue_size} max"
            )

        batch_id = _new_id()
        tasks = [BatchTask(task_id=_new_id(), issue_url=url) for url in issue_urls]
        batch = Batch(batch_id=batch_id, tasks=tasks)
        self._batches[batch_id] = batch

        for task in tasks:
            self._pending.put_nowait((batch_id, task.task_id))

        logger.info("Batch %s submitted with %d tasks", batch_id, len(tasks))
        return batch

    def get_batch(self, batch_id: str) -> Batch | None:
        """Retrieve a batch by ID."""
        return self._batches.get(batch_id)

    async def _worker(self) -> None:
        """Background worker: dequeue and execute tasks one at a time."""
        while self._running:
            try:
                batch_id, task_id = await asyncio.wait_for(self._pending.get(), timeout=1.0)
            except TimeoutError:
                continue

            async with self._semaphore:
                if not self._running:
                    break

                batch = self._batches.get(batch_id)
                task = None
                if batch is not None:
                    for t in batch.tasks:
                        if t.task_id == task_id:
                            task = t
                            break

                if task is None or batch is None:
                    logger.warning("Task %s/%s not found, skipping", batch_id, task_id)
                    continue

                # Update batch + task status
                task.status = "running"
                task.started_at = monotonic()
                if batch.status == "pending":
                    batch.status = "running"

                try:
                    agent = IssueAgent(self._settings, circuit_breaker=self._circuit_breaker)
                    try:
                        task.result = await agent.investigate(task.issue_url)
                        task.status = "completed"
                    finally:
                        await agent.aclose()
                except Exception as exc:
                    task.status = "failed"
                    task.error = str(exc)[:500]
                    logger.exception("Batch task %s failed: %s", task.task_id, task.issue_url)
                finally:
                    task.finished_at = monotonic()

                # Update batch status
                if batch is not None:
                    progress = batch.progress
                    if progress["pending"] == 0 and progress["running"] == 0:
                        batch.status = "completed" if progress["failed"] == 0 else "partial"


def _new_id() -> str:
    """Generate a short random ID (12 hex chars)."""
    return secrets.token_hex(6)
