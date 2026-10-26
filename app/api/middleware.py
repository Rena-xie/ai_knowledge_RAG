"""请求上下文中间件：request_id、访问日志与请求体大小限制（见 8.1、8.5）。"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_payload
from app.core.context import bind_request_id
from app.core.errors import ErrorCode
from app.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)
REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """为每个请求生成 request_id 并记录访问日志。"""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id

        with bind_request_id(request_id):
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


class BodySizeLimitMiddleware:
    """按实际读取的字节数限制请求体大小（见《项目开发说明_v1.md》8.5）。

    先按 `Content-Length` 快速拒绝声明超限的请求；对未声明长度（分块编码）
    或声明不实的请求，在转发给应用前累计实际字节数，超限直接返回 413。
    读取到的请求体在上限内缓存后一次性重放给下游应用（上限默认 1 MiB）。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings_provider: Callable[[], Settings] = get_settings,
    ) -> None:
        self._app = app
        self._settings_provider = settings_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        limit = self._settings_provider().max_request_bytes
        if _declared_too_large(scope, limit):
            await self._reject(scope, receive, send)
            return

        buffered = bytearray()
        terminal: Message | None = None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # 客户端中断等非请求体消息：不再向应用交付半截请求体。
                terminal = message
                break
            buffered.extend(message.get("body", b""))
            if len(buffered) > limit:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        if terminal is not None:

            async def replay_receive() -> Message:
                return terminal

        else:
            body = bytes(buffered)
            delivered = False

            async def replay_receive() -> Message:
                nonlocal delivered
                if delivered:
                    return {"type": "http.request", "body": b"", "more_body": False}
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}

        await self._app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        state = scope.get("state") or {}
        request_id = str(state.get("request_id", ""))
        response = JSONResponse(
            status_code=413,
            content=error_payload(
                ErrorCode.REQUEST_TOO_LARGE, "请求体超过大小上限", request_id
            ),
        )
        await response(scope, receive, send)


def _declared_too_large(scope: Scope, limit: int) -> bool:
    content_length = Headers(scope=scope).get("content-length")
    if content_length is None:
        return False
    try:
        return int(content_length) > limit
    except ValueError:
        return True
