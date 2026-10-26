"""来源接口（见《项目开发说明_v1.md》8.2、8.3）。"""

from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient

from app.api.deps import Container
from app.core.settings import get_settings
from app.infra.db import (
    KnowledgeCollection,
    KnowledgeCollectionSource,
    KnowledgeSource,
    SourceTag,
    Tag,
)
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
        html = self._html
        if url.endswith("/b"):
            html = self._html.replace("BM25 指南", "向量检索指南").replace(
                "BM25 是一种稀疏检索算法", "向量检索使用稠密向量"
            )
        return FetchResult(url=url, content_type="text/html", html=html)

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
    assert body["language_code"] == "zh"

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


def test_get_source_content_returns_read_only_tree(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/sources",
        json={"url": "https://example.com/a?utm_source=x"},
    ).json()
    container: Container = client.app.state.container
    with container.session_factory() as session:
        collection = KnowledgeCollection(
            id="preview-collection",
            name="预览集合",
            description="read-only preview",
            is_default=False,
            created_at="2026-10-26T00:00:00Z",
            updated_at="2026-10-26T00:00:00Z",
        )
        tag = Tag(name="preview", created_at="2026-10-26T00:00:00Z")
        session.add_all([collection, tag])
        session.flush()
        session.add_all(
            [
                KnowledgeCollectionSource(
                    collection_id=collection.id,
                    source_id=created["id"],
                    added_at="2026-10-26T00:00:00Z",
                ),
                SourceTag(
                    source_id=created["id"],
                    tag_id=tag.id,
                    added_at="2026-10-26T00:00:00Z",
                ),
            ]
        )
        session.commit()

    response = client.get(f"/api/v1/sources/{created['id']}/content")

    body = response.json()
    assert response.status_code == 200
    assert body["source_id"] == created["id"]
    assert body["submitted_url"].startswith("https://example.com/a")
    assert body["final_url"] == "https://example.com/a"
    assert body["status"] == "completed"
    assert body["language_code"] == "zh"
    assert body["aliases"]
    assert body["collections"][0]["name"] == "预览集合"
    assert [tag["name"] for tag in body["tags"]] == ["preview"]
    assert isinstance(body["tags"][0]["id"], int)
    assert body["page"] == 1
    assert body["size"] == 20
    assert body["parent_total"] >= 1
    assert body["child_total"] >= body["parent_total"]
    assert body["parents"]
    assert body["parents"][0]["children"]
    assert "embedding_text" not in response.text


def test_get_source_content_rejects_not_ready_source(
    client: TestClient,
) -> None:
    container: Container = client.app.state.container
    with container.session_factory() as session:
        source = KnowledgeSource(
            url="https://example.com/pending",
            canonical_url="https://example.com/pending",
            status="pending",
            identity_status="pending",
            created_at="2026-10-26T00:00:00Z",
            updated_at="2026-10-26T00:00:00Z",
        )
        session.add(source)
        session.commit()
        source_id = source.id

    response = client.get(f"/api/v1/sources/{source_id}/content")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_CONTENT_NOT_READY"


def test_get_source_content_rejects_invalid_pagination(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/sources", json={"url": "https://example.com/a"}
    ).json()

    page_response = client.get(
        f"/api/v1/sources/{created['id']}/content",
        params={"page": 0},
    )
    size_response = client.get(
        f"/api/v1/sources/{created['id']}/content",
        params={"size": 21},
    )

    assert page_response.status_code == 400
    assert page_response.json()["error"]["code"] == "INVALID_REQUEST"
    assert size_response.status_code == 400
    assert size_response.json()["error"]["code"] == "INVALID_REQUEST"


def test_get_source_content_rejects_missing_source(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/sources/999/content")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_get_source_content_rejects_completed_source_without_chunks(
    client: TestClient,
) -> None:
    container: Container = client.app.state.container
    with container.session_factory() as session:
        source = KnowledgeSource(
            url="https://example.com/incomplete",
            canonical_url="https://example.com/incomplete",
            status="completed",
            identity_status="confirmed",
            created_at="2026-10-26T00:00:00Z",
            updated_at="2026-10-26T00:00:00Z",
        )
        session.add(source)
        session.commit()
        source_id = source.id

    response = client.get(f"/api/v1/sources/{source_id}/content")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SOURCE_CONTENT_NOT_READY"


def test_source_content_openapi_contract(client: TestClient) -> None:
    openapi = client.get("/openapi.json").json()
    operation = openapi["paths"]["/api/v1/sources/{source_id}/content"]

    assert set(operation) == {"get"}
    assert (
        operation["get"]["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        == "#/components/schemas/SourceContentResponse"
    )
    assert "ChildPreviewResponse" in openapi["components"]["schemas"]


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
