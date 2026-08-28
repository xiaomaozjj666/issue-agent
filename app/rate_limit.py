"""Sliding-window rate limiting middleware.

Per-API-key request throttling to prevent credit exhaustion.  Default: 30 requests
per 60 seconds, controlled via RATE_LIMIT_REQUESTS and RATE_LIMIT_WINDOW_SECONDS.
"""

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.config import get_settings

_rate_window_buckets: dict[str, deque[float]] = defaultdict(deque)
_rate_window_lock = asyncio.Lock()


async def _check_rate_limit(api_key: str) -> None:
    settings = get_settings()
    max_requests = settings.rate_limit_requests
    window_s = settings.rate_limit_window_seconds
    now = time.monotonic()
    async with _rate_window_lock:
        bucket = _rate_window_buckets[api_key]
        # Evict timestamps outside the window
        cutoff = now - window_s
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_requests:
            retry_after = int(bucket[0] + window_s - now + 1)
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded. Retry after {retry_after}s",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
        # 主动清理过期 key，防止字典随 inactive 用户无限膨胀。
        # 阈值 100：日常单实例活跃 API key 通常 < 20，100 足够宽松。
        if len(_rate_window_buckets) > 100:
            stale = [k for k, v in _rate_window_buckets.items() if not v or v[-1] < cutoff]
            for k in stale:
                del _rate_window_buckets[k]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-API-key sliding-window rate limiter."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        # 健康检查、静态资源与 favicon 不消耗 LLM 额度，豁免限流；
        # 否则单次页面加载（HTML+JS/CSS/图表）即消耗约 11 次配额，UI 一刷新就被 429。
        if path == "/health" or path.startswith("/static") or path == "/favicon.ico":
            return await call_next(request)
        settings = get_settings()
        api_key = request.headers.get("X-API-Key", "")
        try:
            # API_KEY 已配置时按 header 值限流；未配置时 X-API-Key 无认证意义，
            # 攻击者可每次换一个随机值绕过限流，因此始终用客户端 IP 兜底
            if settings.api_key and api_key:
                await _check_rate_limit(api_key)
            else:
                client_ip = request.client.host if request.client else "unknown"
                await _check_rate_limit(client_ip)
        except HTTPException as exc:
            # BaseHTTPMiddleware.dispatch 内抛异常会被 ASGI TaskGroup 包装成未处理
            # 异常组 → 浏览器收到 500 而非 429（前端 JS 加载失败）。这里自行构造响应。
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
        return await call_next(request)
