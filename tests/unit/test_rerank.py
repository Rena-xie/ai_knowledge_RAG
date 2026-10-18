"""Rerank API 客户端（v2 M5）。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.infra.rerank import RerankClient, RerankError


def build_client(
    handler: httpx.MockTransport, *, max_retries: int = 1
) -> RerankClient:
    return RerankClient(
        api_base="https://api.example.com/v1",
        api_key="test-key",
        model="test-reranker",
        timeout_s=1,
        max_retries=max_retries,
        transport=handler,
    )


def test_rerank_sorts_by_score_and_sends_contract() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            },
        )

    client = build_client(httpx.MockTransport(handler))
    try:
        hits = client.rerank("BM25", ["候选一", "候选二"])
    finally:
        client.close()

    assert [(hit.index, hit.score) for hit in hits] == [
        (1, 0.9),
        (0, 0.2),
    ]
    assert seen["path"] == "/v1/rerank"
    assert seen["body"] == {
        "model": "test-reranker",
        "query": "BM25",
        "documents": ["候选一", "候选二"],
        "top_n": 2,
        "return_documents": False,
    }


def test_rerank_retries_server_error() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(
            200, json={"results": [{"index": 0, "relevance_score": 0.8}]}
        )

    client = build_client(httpx.MockTransport(handler), max_retries=1)
    try:
        hits = client.rerank("q", ["a"])
    finally:
        client.close()

    assert calls == 2
    assert hits[0].score == 0.8


def test_rerank_rejects_invalid_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    client = build_client(httpx.MockTransport(handler), max_retries=0)
    try:
        with pytest.raises(RerankError):
            client.rerank("q", ["a"])
    finally:
        client.close()
