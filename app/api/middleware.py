"""请求上下文中间件：request_id、访问日志与请求体大小限制（见 8.1、8.5）。"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.api.errors import error_payload
from app.core.errors import ErrorCode
from app.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)
REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """为每个请求生成 request_id、记录访问日志并限制请求体大小。"""

    def __init__(
        self, app, *, settings_provider: Callable[[], Settings] = get_settings
    ) -> None:
        super().__init__(app)
        self._settings_provider = settings_provider

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        settings = self._settings_provider()

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > settings.max_request_bytes
            except ValueError:
                too_large = True
            if too_large:
                return JSONResponse(
                    status_code=413,
                    content=error_payload(
                        ErrorCode.REQUEST_TOO_LARGE, "请求体超过大小上限", request_id
                    ),
                    headers={REQUEST_ID_HEADER: request_id},
                )

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "请求完成 %s %s -> %s",
            request.method,
            request.url.path,
            response.status_code,
            extra={
                "request_id": request_id,
                "stage": "http",
                "duration_ms": duration_ms,
            },
        )
        return response
