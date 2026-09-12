"""Batch analysis endpoints: submit multiple issues and poll progress (``/batch``)."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field

from app.deps import TaskQueueDep
from app.task_queue import Batch

router = APIRouter()


class BatchSubmitRequest(PydanticBaseModel):
    issue_urls: list[str] = Field(max_length=100)


class BatchTaskResponse(PydanticBaseModel):
    task_id: str
    issue_url: str
    status: str
    error: str | None = None
    has_report: bool = False


class BatchStatusResponse(PydanticBaseModel):
    batch_id: str
    status: str
    progress: dict
    tasks: list[BatchTaskResponse]


@router.post("/batch", response_model=BatchStatusResponse)
async def submit_batch(request: BatchSubmitRequest, queue: TaskQueueDep) -> BatchStatusResponse:
    try:
        batch = queue.submit(request.issue_urls)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _batch_response(batch)


@router.get("/batch/{batch_id}", response_model=BatchStatusResponse)
async def get_batch_status(batch_id: str, queue: TaskQueueDep) -> BatchStatusResponse:
    # 优先读内存；进程重启后从 SQLite 恢复的批次也能被查到
    batch = await queue.get_batch_async(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _batch_response(batch)


def _batch_response(batch: Batch) -> BatchStatusResponse:
    return BatchStatusResponse(
        batch_id=batch.batch_id,
        status=batch.status,
        progress=batch.progress,
        tasks=[
            BatchTaskResponse(
                task_id=t.task_id,
                issue_url=t.issue_url,
                status=t.status,
                error=t.error,
                has_report=t.result is not None,
            )
            for t in batch.tasks
        ],
    )
