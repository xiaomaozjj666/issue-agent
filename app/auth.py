"""API key authentication middleware."""

import hmac
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

logger = logging.getLogger(__name__)

# /docs 和 /openapi.json 不在跳过列表中：当 api_key 已设置时，
# Swagger UI 和 OpenAPI schema 也需要认证，防止 API 结构泄露
SKIP_PATHS = {"/health", "/", "/favicon.ico"}

_warned_no_api_key = False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in SKIP_PATHS or request.url.path.startswith("/static"):
            return await call_next(request)

        settings = get_settings()
        if not settings.api_key:
            global _warned_no_api_key
            if not _warned_no_api_key:
                logger.warning(
                    "API_KEY is not set — all endpoints are unauthenticated. "
                    "Set API_KEY in production to require authentication."
                )
                _warned_no_api_key = True
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")
        if api_key is None:
            return JSONResponse(status_code=401, content={"detail": "Missing X-API-Key header"})

        if not hmac.compare_digest(api_key, settings.api_key):
            return JSONResponse(status_code=403, content={"detail": "Invalid API key"})

        return await call_next(request)
