"""API 统一错误处理与请求上下文（见《项目开发说明_v1.md》8.1、8.5）。"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.main import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("MAX_REQUEST_BYTES", "200")
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


def test_response_contains_request_id(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert len(response.headers["X-Request-ID"]) == 32


def test_client_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "req-abc"})

    assert response.headers["X-Request-ID"] == "req-abc"


def test_unknown_route_returns_error_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/not-exists")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


def test_oversized_body_is_rejected(client: TestClient) -> None:
    response = client.post("/health/live", content=b"x" * 500)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_oversized_chunked_body_is_rejected(client: TestClient) -> None:
    """无 Content-Length 的分块请求同样受请求体上限约束。"""

    def chunks():
        yield b'{"url": "'
        yield b"x" * 150
        yield b"y" * 150
        yield b'"}'

    response = client.post("/api/v1/sources", content=chunks())

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
    assert len(response.headers["X-Request-ID"]) == 32


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.com/a",
        "http://[::1",
    ],
)
def test_invalid_url_does_not_echo_secret(
    client: TestClient,
    url: str,
) -> None:
    response = client.post("/api/v1/sources", json={"url": url})

    assert response.status_code == 400
    assert "secret" not in response.text
