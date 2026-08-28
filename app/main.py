"""FastAPI application entry-point: lifespan wiring, middleware, and top-level routes.

Domain endpoints live in ``app.routes`` (analysis / chat / sessions / batch) and are
included below as routers; cross-cutting HTTP concerns live in ``app.rate_limit``
(throttling), ``app.sse`` (streaming keepalives) and ``app.deps`` (DI + request
helpers).  Business logic (session state updates, report formatting, PR apply/
rollback, event persistence) is delegated to ``app.services``.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import Scope

from app.auth import AuthMiddleware
from app.build import get_build_id
from app.circuit_breaker import CircuitBreaker
from app.config import get_settings
from app.errors import CircuitBreakerOpenError
from app.github import GitHubClient
from app.i18n import get_frontend_strings
from app.logging_config import setup_logging
from app.provider import create_openai_client
from app.rate_limit import RateLimitMiddleware
from app.routes import analysis, batch, chat, sessions
from app.sessions import SessionConflictError, SessionManager
from app.task_queue import TaskQueue

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: setup logging, initialize session store, purge stale data."""
    setup_logging()
    # Initialize session manager and attach to app state for DI
    settings = get_settings()
    db_path: str | None = settings.session_db_path
    if db_path == ":memory:":
        db_path = None
    manager = SessionManager(db_path=db_path)
    _app.state.session_manager = manager

    # 熔断器：跨请求共享，追踪 LLM provider 全局健康状态
    breaker = CircuitBreaker(
        threshold=settings.circuit_breaker_threshold,
        recovery=settings.circuit_breaker_recovery,
    )
    _app.state.circuit_breaker = breaker

    # Provider clients are process-scoped so HTTP/TLS pools survive individual
    # analysis and chat requests. Per-request IssueAgent objects remain cheap
    # coordinators and never close these shared clients.
    openai_client = create_openai_client(settings)
    github_client = GitHubClient(
        settings.github_token,
        max_file_bytes=settings.github_max_file_bytes,
        timeout=settings.github_timeout,
        max_retries=settings.github_max_retries,
        cache_ttl=settings.github_cache_ttl_seconds,
        cache_max_entries=settings.github_cache_max_entries,
        cache_max_bytes=settings.github_cache_max_bytes,
        max_tree_entries=settings.github_max_tree_entries,
        close_on_exit=False,
    )
    _app.state.openai_client = openai_client
    _app.state.github_client = github_client

    async def recover_stale_sessions() -> None:
        cutoff = datetime.now(UTC) - timedelta(seconds=settings.session_stale_after_seconds)
        recovered = await manager.recover_stale(cutoff.isoformat(timespec="seconds"))
        if recovered:
            logger.warning("Recovered %d stale running session(s)", recovered)

    # 启动时恢复一次，后续由低频后台任务负责，避免每次历史列表 GET 都写 SQLite。
    try:
        await recover_stale_sessions()
    except Exception:
        logger.warning("Stale session recovery on startup failed; continuing", exc_info=True)

    # Purge old completed/failed sessions on startup
    try:
        purged = await manager.purge_old_sessions(settings.session_retention_days)
        if purged:
            logger.info("Purged %d expired session(s) older than %d days", purged, settings.session_retention_days)
    except Exception:
        logger.warning("Session purge on startup failed; continuing", exc_info=True)

    # 后台定期清理：长期运行的进程每 6 小时重复一次过期会话清理，
    # 避免只依赖启动时的一次性 purge 导致数据库无限增长
    async def periodic_purge() -> None:
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                count = await manager.purge_old_sessions(settings.session_retention_days)
                if count:
                    logger.info("Periodic purge removed %d expired session(s)", count)
            except Exception:
                logger.warning("Periodic session purge failed; will retry next cycle", exc_info=True)

    purge_task = asyncio.create_task(periodic_purge())

    async def periodic_stale_recovery() -> None:
        # 恢复间隔收紧到 60s，配合 session_stale_after_seconds=300，
        # 孤儿 running 会话约 1 分钟内被识别并翻为 interrupted，避免长期显示“正在分析中”。
        interval = max(30, min(60, settings.session_stale_after_seconds // 5))
        while True:
            await asyncio.sleep(interval)
            try:
                await recover_stale_sessions()
            except Exception:
                logger.warning("Periodic stale session recovery failed; will retry", exc_info=True)

    stale_recovery_task = asyncio.create_task(periodic_stale_recovery())

    # 批量分析任务队列：纯 asyncio 实现，无需外部 broker
    task_queue = TaskQueue(
        settings,
        breaker,
        max_concurrent=settings.batch_max_concurrent,
        max_queue_size=settings.batch_max_queue_size,
        max_history=settings.batch_max_history,
        client=openai_client,
        github_client=github_client,
    )
    await task_queue.start()
    _app.state.task_queue = task_queue

    try:
        yield
    finally:
        purge_task.cancel()
        stale_recovery_task.cancel()
        with suppress(asyncio.CancelledError):
            await purge_task
        with suppress(asyncio.CancelledError):
            await stale_recovery_task
        _app.state.task_queue = None
        await task_queue.stop()
        _app.state.session_manager = None
        await manager.close()
        _app.state.openai_client = None
        _app.state.github_client = None
        await openai_client.close()
        await github_client.aclose()


app = FastAPI(title="GitHub Issue Agent", version="0.6.0", lifespan=lifespan)

# 中间件执行顺序：后添加的先执行（洋葱模型外层）。GZip 只包装响应；
# 请求随后先经过 Auth，确保未认证请求不会消耗合法用户的限流配额。
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)

app.include_router(analysis.router)
app.include_router(chat.router)
app.include_router(sessions.router)
app.include_router(batch.router)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR)) if _TEMPLATES_DIR.exists() else None


class VersionedStaticFiles(StaticFiles):
    """Static files with immutable caching for build-versioned asset URLs."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="ignore"))
            if query.get("v") == [get_build_id()]:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
        return response


if _STATIC_DIR.exists():
    app.mount("/static", VersionedStaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.exception_handler(SessionConflictError)
async def session_conflict_handler(request: Request, error: SessionConflictError) -> JSONResponse:
    logger.warning("Session conflict on %s: %s", request.url.path, error)
    return JSONResponse(status_code=409, content={"detail": "Session changed concurrently; reload and try again"})


@app.exception_handler(CircuitBreakerOpenError)
async def circuit_breaker_handler(request: Request, error: CircuitBreakerOpenError) -> JSONResponse:
    logger.warning("Circuit breaker open, rejecting request to %s", request.url.path)
    return JSONResponse(status_code=503, content={"detail": str(error)})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "issue-agent", "build_id": get_build_id()}


@app.get("/i18n")
async def get_i18n(lang: str = Query(default="zh", pattern=r"^(zh|en)$")) -> dict:
    """返回指定语言的全部前端字符串，供应用内语言切换热更新（无需刷新/重启）。"""
    return get_frontend_strings(lang)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if templates is None:
        raise HTTPException(status_code=404, detail="Web UI templates not found")
    settings = get_settings()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "language": settings.language,
            "frontend_strings": get_frontend_strings(settings.language),
            "build_id": get_build_id(),
        },
    )
