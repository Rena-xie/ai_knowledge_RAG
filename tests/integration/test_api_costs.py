"""API 成本聚合查询与请求/任务关联。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.context import bind_job_id, bind_source_id
from app.core.errors import AppError, ErrorCode
from app.domain.cost import ApiUsageAttempt
from app.main import create_app
from app.services.retrieval import RetrievalResult


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_costs_endpoint_aggregates_events(client: TestClient) -> None:
    cost_service = client.app.state.container.cost_service
    with bind_job_id("job-cost"), bind_source_id(9):
        cost_service.record_attempt(
            ApiUsageAttempt(
                operation="embedding",
                model="BAAI/bge-m3",
                input_units=100,
                usage_source="provider",
                success=True,
                attempt=1,
            )
        )

    response = client.get("/api/v1/costs?since_hours=24")

    assert response.status_code == 200
    body = response.json()
    assert body["since_hours"] == 24
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["operation"] == "embedding"
    assert item["request_count"] == 1
    assert item["success_count"] == 1
    assert item["cost_status"] == "unknown_price"


def test_job_response_exposes_associated_costs(client: TestClient) -> None:
    created = client.post(
        "/api/v1/ingestions",
        json={"urls": ["ftp://example.com/a"]},
    ).json()
    cost_service = client.app.state.container.cost_service
    with bind_job_id(created["job_id"]):
        cost_service.record_attempt(
            ApiUsageAttempt(
                operation="embedding",
                model="BAAI/bge-m3",
                input_units=10,
                usage_source="estimated_characters",
                success=True,
                attempt=1,
            )
        )

    response = client.get(f"/api/v1/ingestions/{created['job_id']}")

    assert response.status_code == 200
    assert response.json()["costs"][0]["operation"] == "embedding"


def test_search_response_exposes_request_costs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = client.app.state.container

    def fake_search(_question: str) -> RetrievalResult:
        container.cost_service.record_attempt(
            ApiUsageAttempt(
                operation="embedding",
                model="BAAI/bge-m3",
                input_units=5,
                usage_source="estimated_characters",
                success=True,
                attempt=1,
            )
        )
        return RetrievalResult((), (), (), ())

    monkeypatch.setattr(container.retrieval_service, "search", fake_search)

    response = client.post("/api/v1/search", json={"question": "测试"})

    assert response.status_code == 200
    assert response.json()["costs"][0]["operation"] == "embedding"


def test_budget_rejection_returns_stable_api_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_budget(_question: str) -> RetrievalResult:
        raise AppError(ErrorCode.COST_BUDGET_EXCEEDED, "API 成本预算已用尽")

    monkeypatch.setattr(
        client.app.state.container.retrieval_service,
        "search",
        reject_budget,
    )

    response = client.post("/api/v1/search", json={"question": "测试"})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "COST_BUDGET_EXCEEDED"
