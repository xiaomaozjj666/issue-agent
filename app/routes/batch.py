"""Batch analysis endpoints: submit issues/repositories, poll progress, inspect results.

Batch analysis runs in-process (see ``app.task_queue``).  A submission accepts
concrete issue URLs plus repository URLs; repositories are expanded server-side
into their most recent open issues so the web UI can accept either form.
"""

import re
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field

from app.config import get_settings
from app.deps import GitHubClientDep, TaskQueueDep
from app.github import (
    GitHubClient,
    GitHubError,
    GitHubRateLimitError,
    GitHubResourceError,
    parse_issue_url,
)
from app.models import AnalysisReport
from app.task_queue import Batch

router = APIRouter()

# 单批最多 100 个任务（与 TaskQueue 默认队列容量一致）
MAX_BATCH_TASKS = 100
# 每个仓库展开的 open issue 数量上限，避免一条仓库链接灌满整批
REPO_ISSUE_LIMIT = 10
_MAX_REPOSITORY_URLS = 20

_REPOSITORY_PATH = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class BatchSubmitRequest(PydanticBaseModel):
    issue_urls: list[str] = Field(default_factory=list, max_length=MAX_BATCH_TASKS)
    repository_urls: list[str] = Field(default_factory=list, max_length=_MAX_REPOSITORY_URLS)


class BatchTaskResponse(PydanticBaseModel):
    task_id: str
    issue_url: str
    status: str
    error: str | None = None
    has_report: bool = False


class BatchTaskDetailResponse(BatchTaskResponse):
    report: AnalysisReport | None = None


class BatchStatusResponse(PydanticBaseModel):
    batch_id: str
    status: str
    progress: dict
    tasks: list[BatchTaskResponse]


def parse_repository_url(url: str) -> tuple[str, str]:
    """Parse ``https://github.com/owner/repo`` into ``(owner, repo)``."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError("repository_url must be an https://github.com URL")
    match = _REPOSITORY_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("repository_url must match https://github.com/owner/repo")
    return match.group(1), match.group(2)


@router.post("/batch", response_model=BatchStatusResponse)
async def submit_batch(
    request: BatchSubmitRequest,
    queue: TaskQueueDep,
    github: GitHubClientDep,
) -> BatchStatusResponse:
    issue_urls = _validate_issue_urls(request.issue_urls)
    repositories = _validate_repository_urls(request.repository_urls)
    expanded = await _expand_repositories(repositories, github)
    urls = _dedupe([*issue_urls, *expanded])
    if not urls:
        raise HTTPException(status_code=422, detail="At least one issue URL is required")
    if len(urls) > MAX_BATCH_TASKS:
        raise HTTPException(
            status_code=422,
            detail=f"Batch supports at most {MAX_BATCH_TASKS} tasks, got {len(urls)}",
        )
    try:
        batch = queue.submit(urls)
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


@router.get("/batch/{batch_id}/tasks/{task_id}", response_model=BatchTaskDetailResponse)
async def get_batch_task(batch_id: str, task_id: str, queue: TaskQueueDep) -> BatchTaskDetailResponse:
    batch = await queue.get_batch_async(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    task = next((item for item in batch.tasks if item.task_id == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return BatchTaskDetailResponse(
        task_id=task.task_id,
        issue_url=task.issue_url,
        status=task.status,
        error=task.error,
        has_report=task.result is not None,
        report=task.result,
    )


@router.post("/batch/{batch_id}/retry", response_model=BatchStatusResponse)
async def retry_batch(batch_id: str, queue: TaskQueueDep) -> BatchStatusResponse:
    """Re-enqueue failed or cancelled tasks of an existing batch."""
    try:
        batch = await queue.retry(batch_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _batch_response(batch)


def _validate_issue_urls(issue_urls: list[str]) -> list[str]:
    validated: list[str] = []
    for index, url in enumerate(issue_urls):
        try:
            parse_issue_url(url)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=f"issue_urls[{index}]: {error}") from error
        validated.append(url)
    return validated


def _validate_repository_urls(repository_urls: list[str]) -> list[tuple[str, str]]:
    repositories: list[tuple[str, str]] = []
    for index, url in enumerate(repository_urls):
        try:
            repositories.append(parse_repository_url(url))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=f"repository_urls[{index}]: {error}") from error
    return repositories


async def _expand_repositories(
    repositories: list[tuple[str, str]],
    github: GitHubClient | None,
) -> list[str]:
    """Expand repository URLs into their most recent open issues."""
    if not repositories:
        return []
    client = github
    owns_client = False
    if client is None:
        # 测试或未跑 lifespan 的嵌入式应用：临时建一个短生命周期客户端
        settings = get_settings()
        client = GitHubClient(
            settings.github_token,
            max_file_bytes=settings.github_max_file_bytes,
            timeout=settings.github_timeout,
            max_retries=settings.github_max_retries,
        )
        owns_client = True
    expanded: list[str] = []
    try:
        for owner, repo in repositories:
            try:
                numbers = await client.list_open_issues(owner, repo, REPO_ISSUE_LIMIT)
            except GitHubRateLimitError as error:
                raise HTTPException(
                    status_code=429, detail=f"Cannot list issues for {owner}/{repo}: {error}"
                ) from error
            except GitHubResourceError as error:
                raise HTTPException(
                    status_code=422, detail=f"Cannot list issues for {owner}/{repo}: {error}"
                ) from error
            except GitHubError as error:
                raise HTTPException(
                    status_code=502, detail=f"Cannot list issues for {owner}/{repo}: {error}"
                ) from error
            expanded.extend(f"https://github.com/{owner}/{repo}/issues/{number}" for number in numbers)
    finally:
        if owns_client:
            await client.aclose()
    return expanded


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


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
