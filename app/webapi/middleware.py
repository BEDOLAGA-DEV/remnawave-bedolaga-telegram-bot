from __future__ import annotations

from time import monotonic

import structlog
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from structlog.contextvars import bound_contextvars


logger = structlog.get_logger('web_api')


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Логирование входящих запросов в административный API."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        with bound_contextvars(http_method=request.method, http_path=request.url.path):
            start = monotonic()
            response: Response | None = None
            try:
                response = await call_next(request)
                return response
            except (TimeoutError, ConnectionRefusedError, OSError, OperationalError, InterfaceError) as e:
                logger.error(
                    'Database connection error while handling request',
                    method=request.method,
                    path=request.url.path,
                    e=str(e)[:200],
                )
                response = JSONResponse(
                    status_code=503,
                    content={'detail': 'Service temporarily unavailable. Please try again later.'},
                )
                return response
            finally:
                duration_ms = (monotonic() - start) * 1000
                status = response.status_code if response else 'error'
                logger.debug(
                    'Request handled',
                    method=request.method,
                    path=request.url.path,
                    status=status,
                    duration_ms=duration_ms,
                )
