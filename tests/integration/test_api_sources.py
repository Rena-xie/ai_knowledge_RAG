"""来源接口（见《项目介绍.md》8.2、8.3）。"""

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
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    def close(self) -> None:
        return None


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = get_settings()
    container = Container(
        settings, fetcher=FakeFetcher(ARTICLE), embedder=FakeEmbedder()
    )
    app = create_app()
    app.state.container = container
    with TestClient(app) as test_client:
        yield test_client


def test_submit_source_ingests_and_deduplicates(client: TestClient) -> None:
    first = client.post("/api/v1/sources", json={"url": "https://example.com/a?utm_source=x"})

    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "completed"
    assert body["triggered"] is True
    assert body["canonical_url"] == "https://example.com/a"

    second = client.post("/api/v1/sources", json={"url": "https://example.com/a"})

    assert second.status_code == 200
    assert second.json()["id"] == body["id"]
    assert second.json()["triggered"] is False


def test_submit_source_rejects_invalid_url(client: TestClient) -> None:
    response = client.post("/api/v1/sources", json={"url": "ftp://example.com/a"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_submit_source_rejects_missing_url(client: TestClient) -> None:
    response = client.post("/api/v1/sources", json={})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_list_sources_paginates(client: TestClient) -> None:
    client.post("/api/v1/sources", json={"url": "https://example.com/a"})
    client.post("/api/v1/sources", json={"url": "https://example.com/b"})

    response = client.get("/api/v1/sources", params={"page": 1, "size": 1})

    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["page"] == 1
    assert body["size"] == 1


def test_get_missing_source_returns_not_found(client: TestClient) -> None:
    response = client.get("/api/v1/sources/999")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_delete_source_cascades(client: TestClient) -> None:
    created = client.post(
        "/api/v1/sources", json={"url": "https://example.com/a"}
    ).json()
    container: Container = client.app.state.container
    assert container.vector_index.count() > 0

    response = client.delete(f"/api/v1/sources/{created['id']}")

    assert response.status_code == 204
    assert client.get(f"/api/v1/sources/{created['id']}").status_code == 404
    assert container.vector_index.count() == 0
