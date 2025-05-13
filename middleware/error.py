"""
omnichat.middleware.error
~~~~~~~~~~~~~~~~~~~~~~~~~
FastAPI middleware that:

• catches all uncaught exceptions
• logs stack trace via loguru
• sends event to Sentry (if SENTRY_DSN present)
• returns uniform JSON error with trace-id

Add to FastAPI before any routes:

    from omnichat.middleware.error import ErrorMiddleware
    app.add_middleware(ErrorMiddleware)
"""

from __future__ import annotations

import secrets
import traceback
from datetime import datetime
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from omnichat_log import logger
from settings import settings

try:
    import sentry_sdk  # lazy import
except ImportError:  # Sentry optional
    sentry_sdk = None


class ErrorMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)
        if sentry_sdk and settings.model_dump().get("sentry_dsn"):
            sentry_sdk.init(
                dsn=settings.model_dump()["sentry_dsn"],
                traces_sample_rate=0.0,  # metrics elsewhere; change if needed
                environment=settings.env,
            )

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)

        except Exception as exc:  # pylint: disable=broad-except
            error_id = secrets.token_hex(8)
            tb_str = "".join(traceback.format_exception(exc))
            logger.exception(f"Unhandled exception [{error_id}]\n{tb_str}")

            if sentry_sdk and sentry_sdk.Hub.current.client:
                with sentry_sdk.push_scope() as scope:
                    scope.set_tag("error_id", error_id)
                    scope.set_tag("path", request.url.path)
                    scope.set_extra("body", await _peek_body(request))
                    sentry_sdk.capture_exception(exc)

            payload = {
                "detail": "Internal Server Error",
                "error_id": error_id,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
            return JSONResponse(payload, status_code=500)


async def _peek_body(request: Request) -> str | None:
    """Return request body without consuming it (best-effort)."""
    if "content-type" in request.headers and "application/json" in request.headers["content-type"].lower():
        body = await request.body()
        return body.decode()[:800]  # cap size
    return None
