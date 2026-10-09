"""Embedding 客户端（见《项目介绍.md》10.4）。"""

import json

import httpx
import pytest

from app.core.errors import ErrorCode
from app.infra.embedding import EmbeddingClient, EmbeddingError

API_BASE = "https://api.example.com/v1"


def make_client(handler, **overrides) -> EmbeddingClient:
    options = {
        "api_base": API_BASE,
        "api_key": "test-key",
        "model": "BAAI/bge-m3",
        "timeout_s": 1,
        "max_retries": 0,
        "backoff_s": 0.0,
    }
    options.update(overrides)
    return EmbeddingClient(transport=httpx.MockTransport(handler), **options)


def vector_response(vectors: list[tuple[int, list[float]]]) -> httpx.Response:
    payload = {
        "data": [
            {"index": index, "embedding": embedding} for index, embedding in vectors
        ]
    }
    return httpx.Response(200, json=payload)


def test_orders_vectors_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        count = len(body["input"])
        vectors = [(index, [float(index)]) for index in reversed(range(count))]
        return vector_response(vectors)

    result = make_client(handler).embed(["甲", "乙", "丙"])

    assert result == [[0.0], [1.0], [2.0]]


def test_sends_authorization_and_model() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return vector_response([(0, [0.1])])

    make_client(handler).embed(["甲"])

    assert captured["url"] == f"{API_BASE}/embeddings"
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"] == {"model": "BAAI/bge-m3", "input": ["甲"]}


def test_splits_large_input_into_batches() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        count = len(body["input"])
        calls.append(count)
        return vector_response([(index, [float(index)]) for index in range(count)])

    result = make_client(handler).embed([f"文本{index}" for index in range(33)])

    assert calls == [32, 1]
    assert len(result) == 33


def test_retries_on_rate_limit() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429)
        return vector_response([(0, [0.5])])

    result = make_client(handler, max_retries=1).embed(["甲"])

    assert result == [[0.5]]
    assert len(calls) == 2


def test_raises_after_retries_exhausted() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    with pytest.raises(EmbeddingError) as excinfo:
        make_client(handler, max_retries=1).embed(["甲"])

    assert excinfo.value.code == ErrorCode.EMBEDDING_FAILED
    assert len(calls) == 2


def test_timeout_raises_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("boom")

    with pytest.raises(EmbeddingError) as excinfo:
        make_client(handler).embed(["甲"])

    assert excinfo.value.code == ErrorCode.EMBEDDING_FAILED
