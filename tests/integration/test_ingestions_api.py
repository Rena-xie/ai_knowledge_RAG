"""异步入库任务 API（v2 M1）。"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient

from app.api.deps import Container
from app.core.errors import ErrorCode
from app.core.settings import get_settings
from app.infra.fetcher import FetchError, FetchResult
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

TERMINAL = {"completed", "partial", "failed"}


class FakeFetcher:
    def fetch(self, url: str) -> FetchResult:
        if "fail" in url:
            raise FetchError(ErrorCode.FETCH_TIMEOUT, "抓取超时")
        return FetchResult(url=url, content_type="text/html", html=ARTICLE)

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
        settings, fetcher=FakeFetcher(), embedder=FakeEmbedder()
    )
    app = create_app()
    app.state.container = container
    with TestClient(app) as test_client:
        yield test_client


def wait_for_job(
    client: TestClient, job_id: str, *, timeout_s: float = 10.0
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/ingestions/{job_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] in TERMINAL:
            return last
        time.sleep(0.05)
    raise AssertionError(f"job 未在超时内完成: {last}")


def test_batch_ingestion_completes(client: TestClient) -> None:
    response = client.post(
        "/api/v1/ingestions",
        json={
            "urls": [
                "https://example.com/a?utm_source=x",
                "https://example.com/b",
            ]
        },
    )

    assert response.status_code == 202
    created = response.json()
    assert created["accepted"] == 2
    assert created["total"] == 2

    job = wait_for_job(client, created["job_id"])
    assert job["status"] == "completed"
    assert job["succeeded"] == 2
    assert job["failed"] == 0
    assert [item["status"] for item in job["items"]] == [
        "completed",
        "completed",
    ]
    assert all(item["source_id"] is not None for item in job["items"])
    assert job["items"][0]["canonical_url"] == "https://example.com/a"

    sources = client.get("/api/v1/sources").json()
    assert sources["total"] == 2


def test_duplicate_url_is_skipped(client: TestClient) -> None:
    first = client.post(
        "/api/v1/ingestions", json={"urls": ["https://example.com/a"]}
    ).json()
    wait_for_job(client, first["job_id"])

    second = client.post(
        "/api/v1/ingestions", json={"urls": ["https://example.com/a"]}
    )

    assert second.status_code == 200
    body = second.json()
    assert body["accepted"] == 0
    assert body["skipped"][0]["reason"] == "already_indexed"
    assert body["skipped"][0]["source_id"] is not None


def test_partial_failure_and_retry(client: TestClient) -> None:
    response = client.post(
        "/api/v1/ingestions",
        json={
            "urls": [
                "https://example.com/good",
                "https://example.com/fail",
            ]
        },
    )
    created = response.json()
    job = wait_for_job(client, created["job_id"])

    assert job["status"] == "partial"
    assert job["succeeded"] == 1
    assert job["failed"] == 1
    failed = next(item for item in job["items"] if item["status"] == "failed")
    assert failed["error_code"] == ErrorCode.FETCH_TIMEOUT.value

    retried = client.post(f"/api/v1/ingestions/{created['job_id']}/retry")
    assert retried.status_code == 202
    retried_body = retried.json()
    assert retried_body["job_id"] != created["job_id"]
    assert retried_body["accepted"] == 1
    retried_job = wait_for_job(client, retried_body["job_id"])
    assert retried_job["status"] == "failed"


def test_invalid_url_is_partial_failure(client: TestClient) -> None:
    response = client.post(
        "/api/v1/ingestions",
        json={"urls": ["ftp://example.com/a", "https://example.com/b"]},
    )
    assert response.status_code == 202
    created = response.json()
    assert created["total"] == 2
    assert created["accepted"] == 1

    job = wait_for_job(client, created["job_id"])
    assert job["status"] == "partial"
    invalid = next(item for item in job["items"] if item["status"] == "failed")
    assert invalid["error_code"] == ErrorCode.INVALID_REQUEST.value


def test_job_not_found(client: TestClient) -> None:
    response = client.get("/api/v1/ingestions/job_missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.JOB_NOT_FOUND.value


def test_too_many_urls(client: TestClient) -> None:
    urls = [f"https://example.com/{index}" for index in range(21)]

    response = client.post("/api/v1/ingestions", json={"urls": urls})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.TOO_MANY_URLS.value


def test_idempotency_key_replays_job(client: TestClient) -> None:
    headers = {"Idempotency-Key": "same-key"}
    first = client.post(
        "/api/v1/ingestions",
        json={"urls": ["https://example.com/a"]},
        headers=headers,
    )
    second = client.post(
        "/api/v1/ingestions",
        json={"urls": ["https://example.com/a"]},
        headers=headers,
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["accepted"] == 0


def test_list_ingestions(client: TestClient) -> None:
    client.post("/api/v1/ingestions", json={"urls": ["https://example.com/a"]})

    response = client.get("/api/v1/ingestions")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["job_id"].startswith("job_")
