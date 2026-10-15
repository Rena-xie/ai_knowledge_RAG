"""请求体大小限制中间件（见《项目介绍.md》8.5）。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

from app.api.middleware import BodySizeLimitMiddleware
from app.core.settings import Settings

LIMIT = 200


def _settings() -> Settings:
    return Settings(
        embedding_api_base="https://example.invalid/v1",
        embedding_api_key="test-key",
        llm_api_base="https://example.invalid/v1",
        llm_api_key="test-key",
        max_request_bytes=LIMIT,
    )


class _BodyReaderApp:
    """最简 ASGI 应用：读完整请求体后返回 200，用于观测 receive 行为。"""

    def __init__(self) -> None:
        self.body = b""

    async def __call__(self, scope, receive, send) -> None:
        while True:
            message = await receive()
            self.body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _receive_from(chunks: list[bytes]):
    queue: Iterator[bytes] = iter(chunks)

    async def receive() -> dict[str, Any]:
        try:
            chunk = next(queue)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": chunk, "more_body": True}

    return receive


def _scope(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
        "state": {"request_id": "req-test"},
    }


def _run(middleware: BodySizeLimitMiddleware, scope, receive, sent: list[dict]):
    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))


def test_chunked_body_under_limit_passes_through() -> None:
    app = _BodyReaderApp()
    middleware = BodySizeLimitMiddleware(app, settings_provider=_settings)
    sent: list[dict] = []

    _run(
        middleware,
        _scope([]),
        _receive_from([b"x" * 100, b"y" * 50]),
        sent,
    )

    assert app.body == b"x" * 100 + b"y" * 50
    assert sent[0]["status"] == 200


def test_chunked_body_over_limit_rejected_without_calling_app() -> None:
    """未声明 Content-Length 的分块请求按实际字节数拦截。"""

    app = _BodyReaderApp()
    middleware = BodySizeLimitMiddleware(app, settings_provider=_settings)
    sent: list[dict] = []

    _run(
        middleware,
        _scope([(b"transfer-encoding", b"chunked")]),
        _receive_from([b"x" * 150, b"y" * 150]),
        sent,
    )

    assert app.body == b""
    assert sent[0]["status"] == 413
    body = json.loads(sent[1]["body"])
    assert body["error"]["code"] == "REQUEST_TOO_LARGE"
    assert body["error"]["request_id"] == "req-test"


def test_declared_content_length_over_limit_rejected_without_reading() -> None:
    app = _BodyReaderApp()
    middleware = BodySizeLimitMiddleware(app, settings_provider=_settings)
    sent: list[dict] = []

    _run(
        middleware,
        _scope([(b"content-length", str(LIMIT + 1).encode())]),
        _receive_from([b"x" * (LIMIT + 1)]),
        sent,
    )

    assert app.body == b""
    assert sent[0]["status"] == 413


def test_invalid_content_length_is_rejected() -> None:
    app = _BodyReaderApp()
    middleware = BodySizeLimitMiddleware(app, settings_provider=_settings)
    sent: list[dict] = []

    _run(
        middleware,
        _scope([(b"content-length", b"not-a-number")]),
        _receive_from([b"x"]),
        sent,
    )

    assert sent[0]["status"] == 413
