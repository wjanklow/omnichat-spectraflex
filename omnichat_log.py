"""
omnichat.logging
~~~~~~~~~~~~~~~~
Centralised Loguru configuration:
• DEV  → pretty-printed, colorised logs
• STAGE/PROD → compact JSON for Loki / Datadog / CloudWatch
• Uvicorn & FastAPI access logs routed through Loguru
Usage:
    from omnichat.logging import logger          # already configured at import-time
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from types import FrameType
from typing import Any, Dict

from loguru import logger
from starlette.requests import Request
from starlette.responses import Response

from settings import settings

# ── 1.  Remove default handlers so we fully control output ─────────────────────
logger.remove()

DEF_FMT_DEV = (
    "<green>{time:HH:mm:ss.SSS}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{name}:{function}:{line}</cyan> "
    "» <level>{message}</level>"
)

def _json_sink(message: Dict[str, Any]) -> None:
    """Sink that prints loguru record as one-line JSON."""
    record = message.record
    out = {
        "timestamp": record["time"].strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "level": record["level"].name,
        "msg": record["message"],
        "module": record["name"],
        "fn": record["function"],
        "line": record["line"],
    }
    if record["exception"]:
        out["exc"] = record["exception"].repr
    print(json.dumps(out), file=sys.stdout, flush=True)

def _patch_uvicorn_logging() -> None:
    """Replace Uvicorn's loggers with Loguru so gunicorn -k uvicorn works too."""
    class InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            depth = 2
            frame: FrameType | None = sys._getframe(depth)
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1
            logger.opt(depth=depth, exception=record.exc_info).log(
                record.levelname, record.getMessage()
            )
    logging.root.handlers = [InterceptHandler()]
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = [InterceptHandler()]

# ── 2.  Attach appropriate sinks ───────────────────────────────────────────────
if settings.env == "dev":
    logger.add(
        sys.stdout,
        level=settings.log_level,
        format=DEF_FMT_DEV,
        backtrace=True,
        diagnose=True,
    )
else:  # staging / prod
    logger.add(
        _json_sink,
        level=settings.log_level,
        backtrace=False,
        diagnose=False,
    )

_patch_uvicorn_logging()

# ── 3.  Convenience FastAPI middleware for request tracing ────────────────────
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = datetime.now()
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            duration_ms = (datetime.now() - start).total_seconds() * 1000
            logger.bind(
                method=request.method,
                path=request.url.path,
                status=response.status_code if response else "ERROR",
                dur=f"{duration_ms:.1f}ms",
                client=request.client.host,
            ).info("HTTP")

# Re-export for easy import in app.py
__all__ = ["logger", "RequestLogMiddleware"]
