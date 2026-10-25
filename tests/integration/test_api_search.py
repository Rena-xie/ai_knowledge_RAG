"""检索接口。"""

from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient

from app.api.deps import Container
from app.core.settings import get_settings
from app.infra.fetcher import FetchResult
from app.main import create_app

ARTICLE = """
<html><head><title>BM25 指南</title></head><body><article>
<h1>BM25 指南</h1>
<p>BM25 是一种稀疏检索算法，通过对词频和文档长度归一化来计算相关性得分。</p>
<h2>适用场景</h2>
<p>当查询包含专有名词或代码函数名时，BM25 往往比向量检索更准确。</p>
<p>混合检索使用倒数排名融合算法合并两路结果，兼顾语义召回与关键词召回。</p>
</article></body></html>
"""


class FakeFetcher:
    def __init__(self, html: str) -> None:
        self._html = html

    def fetch(self, url: str) -> FetchResult:
        return FetchResult(url=url, content_type="text/html", html=self._html)

    def close(self) -> None:
        return None


class FakeEmbedder:
    def embed(
        self, texts: Sequence[str], *, budget_s: float | None = None
    ) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    def close(self) -> None:
        return None


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = get_settings()
    container = Container(
        settings,
        fetcher=FakeFetcher(ARTICLE),
        embedder=FakeEmbedder(),
    )
    app = create_app()
    app.state.container = container
    with TestClient(app) as test_client:
        yield test_client


def test_search_returns_three_way_results(client: TestClient) -> None:
    client.post("/api/v1/sources", json={"url": "https://example.com/a"})

    response = client.post("/api/v1/search", json={"question": "BM25 是什么"})

    body = response.json()
    assert response.status_code == 200
    assert body["dense_ids"]
    assert body["bm25_ids"]
    assert body["fused_ids"]
    assert body["evidences"]
    assert body["evidences"][0]["score"] > 0
    assert body["evidences"][0]["text"]
    assert body["elapsed_ms"] >= 0


def test_question_is_required(client: TestClient) -> None:
    response = client.post("/api/v1/search", json={"question": ""})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_query_endpoint_removed(client: TestClient) -> None:
    response = client.post("/api/v1/query", json={"question": "BM25"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
