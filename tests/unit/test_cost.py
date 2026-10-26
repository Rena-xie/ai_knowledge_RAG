"""API 用量、成本聚合与预算门禁。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from sqlalchemy import Engine

from app.core.context import bind_job_id, bind_request_id, bind_source_id
from app.core.errors import ErrorCode
from app.core.settings import Settings
from app.domain.cost import (
    ApiUsageAttempt,
    CostBudgetExceeded,
    CostUsageObserver,
)
from app.infra.db import create_db_engine, create_session_factory, init_db
from app.infra.embedding import EmbeddingClient, EmbeddingError
from app.infra.rerank import RerankClient
from app.services.cost import CostScope, CostService


def build_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "embedding_api_base": "https://example.invalid/v1",
        "embedding_api_key": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_engine = create_db_engine(tmp_path / "app.db")
    init_db(db_engine)
    yield db_engine
    db_engine.dispose()


class RecordingObserver:
    """收集客户端上报的尝试，并可模拟预算拒绝。"""

    provider = "test-provider"

    def __init__(self, *, reject_budget: bool = False) -> None:
        self.events: list[ApiUsageAttempt] = []
        self.reject_budget = reject_budget

    def check_budget(self, _operation: str, _input_units: int) -> None:
        if self.reject_budget:
            raise CostBudgetExceeded("预算不足")

    def record_attempt(self, attempt: ApiUsageAttempt) -> None:
        self.events.append(attempt)


def make_embedding_client(
    handler,
    observer: CostUsageObserver,
    *,
    max_retries: int = 0,
) -> EmbeddingClient:
    return EmbeddingClient(
        api_base="https://example.invalid/v1",
        api_key="secret",
        model="embedding-test",
        timeout_s=1,
        max_retries=max_retries,
        backoff_s=0,
        transport=httpx.MockTransport(handler),
        usage_observer=observer,
    )


def test_embedding_records_provider_usage(engine: Engine) -> None:
    observer = RecordingObserver()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0, 0.0]}],
                "usage": {"prompt_tokens": 12, "total_tokens": 12},
            },
        )

    make_embedding_client(handler, observer).embed(["测试"])

    assert observer.events == [
        ApiUsageAttempt(
            operation="embedding",
            model="embedding-test",
            input_units=12,
            usage_source="provider",
            success=True,
            attempt=1,
        )
    ]


def test_embedding_retry_counts_timeout_and_success(
    engine: Engine,
) -> None:
    observer = RecordingObserver()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("timeout")
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0]}]},
        )

    make_embedding_client(
        handler,
        observer,
        max_retries=1,
    ).embed(["测试"])

    assert len(observer.events) == 2
    assert observer.events[0].success is False
    assert observer.events[0].timed_out is True
    assert observer.events[0].attempt == 1
    assert observer.events[1].success is True
    assert observer.events[1].attempt == 2
    assert observer.events[1].usage_source == "estimated_characters"


def test_rerank_records_estimated_usage_without_provider_usage() -> None:
    observer = RecordingObserver()
    client = RerankClient(
        api_base="https://example.invalid/v1",
        api_key="secret",
        model="rerank-test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"results": [{"index": 0, "relevance_score": 0.9}]},
            )
        ),
        usage_observer=observer,
    )

    client.rerank("查询", ["候选"])

    assert observer.events[0].operation == "rerank"
    assert observer.events[0].usage_source == "estimated_characters"
    assert observer.events[0].input_units == len("查询") + len("候选")


def test_budget_rejection_translates_to_stable_error() -> None:
    observer = RecordingObserver(reject_budget=True)
    client = make_embedding_client(
        lambda _request: httpx.Response(200),
        observer,
    )

    with pytest.raises(EmbeddingError) as exc_info:
        client.embed(["测试"])

    assert exc_info.value.code is ErrorCode.COST_BUDGET_EXCEEDED


def test_cost_service_aggregates_and_enforces_budget(engine: Engine) -> None:
    settings = build_settings(
        embedding_input_price_per_million=2.0,
        api_cost_budget_24h=0.01,
    )
    service = CostService(
        session_factory=create_session_factory(engine),
        settings=settings,
        clock=now_iso,
    )
    with (
        bind_request_id("req-1"),
        bind_job_id("job-1"),
        bind_source_id(7),
    ):
        service.record_attempt(
            ApiUsageAttempt(
                operation="embedding",
                model="embedding-test",
                input_units=1000,
                usage_source="provider",
                success=False,
                attempt=1,
                timed_out=True,
                error_code="EMBEDDING_FAILED",
            )
        )
        service.record_attempt(
            ApiUsageAttempt(
                operation="embedding",
                model="embedding-test",
                input_units=2000,
                usage_source="estimated_characters",
                success=True,
                attempt=2,
            )
        )

    summary = service.summary(CostScope(job_id="job-1"))
    assert len(summary) == 2
    total_requests = sum(item.request_count for item in summary)
    total_input_units = sum(item.input_units for item in summary)
    total_cost = sum(cast(float, item.cost) for item in summary)
    assert total_requests == 2
    assert total_input_units == 3000
    assert total_cost == pytest.approx(0.006)
    assert sum(item.failure_count for item in summary) == 1
    assert sum(item.retry_count for item in summary) == 1
    assert sum(item.timeout_count for item in summary) == 1
    with pytest.raises(CostBudgetExceeded):
        service.check_budget("embedding", 3_000_000)


def test_cost_service_marks_unknown_price(engine: Engine) -> None:
    service = CostService(
        session_factory=create_session_factory(engine),
        settings=build_settings(),
        clock=now_iso,
    )
    service.record_attempt(
        ApiUsageAttempt(
            operation="embedding",
            model="embedding-test",
            input_units=100,
            usage_source="estimated_characters",
            success=True,
            attempt=1,
        )
    )

    summary = service.summary(CostScope())

    assert summary[0].cost is None
    assert summary[0].uncosted_count == 1
