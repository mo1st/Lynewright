from __future__ import annotations

import time
import uuid
import hashlib

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.services.application_logging import bind_request_context, log_event, reset_request_context

# A public origin is scanned constantly. These requests say nothing about the
# application, and at ~5k retained rows they were evicting the errors an
# operator actually opens the log to find.
_PROBE_PATHS = frozenset({
    "/", "/favicon.ico", "/robots.txt", "/sitemap.xml", "/health", "/healthz",
    "/metrics", "/login", "/mcp", "/sse", "/jsonrpc", "/v1/metadata", "/wp-login.php",
    "/.env", "/.git/config", "/Console/Version", "/login.action", "/login.do",
})
_PROBE_PREFIXES = ("/.well-known/", "/wp-", "/vendor/", "/cgi-bin/")


def is_noise(path: str, status_code: int) -> bool:
    """True for internet background noise: a 404 on a non-API probe path."""
    if status_code != 404 or path.startswith("/api/"):
        return False
    if ".." in path or "etc/passwd" in path:
        return True
    return path in _PROBE_PATHS or path.startswith(_PROBE_PREFIXES)


def _safe_log_path(path: str) -> str:
    prefix = "/api/media/l/"
    if path.startswith(prefix):
        token = path[len(prefix):].split("/", 1)[0]
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        suffix = path[len(prefix) + len(token):]
        return f"{prefix}sha256-{digest}{suffix}"
    return path


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        request.state.correlation_id = correlation_id
        safe_path = _safe_log_path(request.url.path)
        # Bound before call_next so every log written while handling this request
        # — however deep — carries the same correlation id and path.
        token = bind_request_context(
            correlation_id=correlation_id,
            request_path=safe_path,
            http_method=request.method,
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            log_event(
                level="critical", category="http", source="backend.middleware",
                message=f"Unhandled {type(exc).__name__}: {exc}",
                details={
                    "method": request.method,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
                exc=exc, correlation_id=correlation_id, request_path=safe_path,
            )
            raise
        finally:
            reset_request_context(token)
        response.headers["X-Correlation-ID"] = correlation_id
        path = safe_path
        if response.status_code >= 400 and not path.startswith("/api/logs"):
            # Query strings are never logged: `public_token` travels in one.
            log_event(
                level="debug" if is_noise(path, response.status_code)
                else "error" if response.status_code >= 500 else "warning",
                category="http", source="backend.middleware",
                message=f"{request.method} {path} returned {response.status_code}",
                details={
                    "method": request.method,
                    "status_code": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    # Set by the HTTPException/validation handlers, so the log
                    # says *why* the request failed, not only that it did.
                    **(getattr(request.state, "failure_details", None) or {}),
                },
                correlation_id=correlation_id, request_path=path,
            )
        return response
