"""API key authentication middleware."""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

SKIP_PATHS = {"/health", "/", "/docs", "/openapi.json", "/favicon.ico"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in SKIP_PATHS or request.url.path.startswith("/static"):
            return await call_next(request)

        settings = get_settings()
        if not settings.api_key:
            return await call_next(request)

        api_key = request.headers.get("X-API-Key")
        if api_key is None:
            return JSONResponse(status_code=401, content={"detail": "Missing X-API-Key header"})

        import hmac
        if not hmac.compare_digest(api_key, settings.api_key):
            return JSONResponse(status_code=403, content={"detail": "Invalid API key"})

        return await call_next(request)
