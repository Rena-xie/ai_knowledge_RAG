"""异步入库任务与 Worker（v2 M1）。"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import AppError, ErrorCode
from app.core.settings import Settings
from app.infra.db import (
    IngestionJob,
    IngestionJobItem,
    KnowledgeSource,
    create_db_engine,
    create_session_factory,
    init_db,
)
from app.services.ingestion import IngestionService, IngestResult
from app.services.ingestion_jobs import ItemStatus, JobService, JobStatus

NOW = "2026-10-19T00:00:00Z"


def build_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "embedding_api_base": "https://example.invalid/v1",
        "embedding_api_key": "test",
        "llm_api_base": "https://example.invalid/v1",
        "llm_api_key": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeIngestion:
    """按 URL 返回预设结果或异常的入库替身。"""

    def __init__(self, results: dict[str, IngestResult | Exception]) -> None:
        self._results = results
        self.calls: list[str] = []

    def ingest(
        self, url: str, *, force_refresh: bool = False
    ) -> IngestResult:
        self.calls.append(url)
        result = self._results[url]
        if isinstance(result, Exception):
            raise result
        return result


@dataclass(frozen=True)
class Env:
    session_factory: sessionmaker[Session]
    settings: Settings


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    engine = create_db_engine(tmp_path / "app.db")
    init_db(engine)
    yield Env(
        session_factory=create_session_factory(engine),
        settings=build_settings(),
    )
    engine.dispose()


def add_source(
    session_factory: sessionmaker[Session],
    canonical_url: str,
    *,
    status: str = "completed",
) -> int:
    with session_factory() as session:
        source = KnowledgeSource(
            url=canonical_url,
            canonical_url=canonical_url,
            title="示例",
            status=status,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(source)
        session.commit()
        return source.id


def build_service(
    env: Env,
    ingestion: FakeIngestion,
    *,
    settings: Settings | None = None,
) -> JobService:
    counter = itertools.count(1)
    return JobService(
        session_factory=env.session_factory,
        ingestion_service=cast(IngestionService, ingestion),
        settings=settings or env.settings,
        clock=lambda: NOW,
        id_factory=lambda: f"job_{next(counter)}",
    )


def test_create_job_dedupes_and_skips_completed(env: Env) -> None:
    source_id = add_source(env.session_factory, "https://example.com/a")
    service = build_service(env, FakeIngestion({}))
    enqueued: list[str] = []

    result = service.create_job(
        [
            "https://example.com/a?utm_source=x",
            "https://example.com/b",
            "https://example.com/b",
        ],
        enqueue=enqueued.append,
    )

    assert result.total == 2
    assert result.accepted == 1
    assert result.skipped[0].reason == "already_indexed"
    assert result.skipped[0].source_id == source_id
    assert enqueued == [result.job_id]
    view = service.get_job(result.job_id)
    assert view.status == JobStatus.QUEUED
    assert len(view.items) == 2


def test_invalid_url_is_failed_item(env: Env) -> None:
    service = build_service(env, FakeIngestion({}))
    enqueued: list[str] = []

    result = service.create_job(
        ["ftp://example.com/a", "https://example.com/b"],
        enqueue=enqueued.append,
    )

    assert result.total == 2
    assert result.accepted == 1
    view = service.get_job(result.job_id)
    statuses = {item.url: item.status for item in view.items}
    assert statuses["ftp://example.com/a"] == ItemStatus.FAILED.value
    assert view.failed == 1
    assert view.status == JobStatus.QUEUED


def test_run_job_isolates_failures(env: Env) -> None:
    good_url = "https://example.com/good"
    bad_url = "https://example.com/bad"
    source_id = add_source(env.session_factory, good_url, status="failed")
    ingestion = FakeIngestion(
        {
            good_url: IngestResult(
                source_id=source_id,
                status="completed",
                triggered=True,
                child_count=2,
            ),
            bad_url: AppError(ErrorCode.FETCH_TIMEOUT, "抓取超时"),
        }
    )
    service = build_service(env, ingestion)
    result = service.create_job([good_url, bad_url], enqueue=lambda _: None)

    service.run_job(result.job_id)

    view = service.get_job(result.job_id)
    assert view.status == JobStatus.PARTIAL
    assert view.succeeded == 1
    assert view.failed == 1
    by_url = {item.url: item for item in view.items}
    assert by_url[good_url].status == ItemStatus.COMPLETED.value
    assert by_url[bad_url].error_code == ErrorCode.FETCH_TIMEOUT.value


def test_retry_creates_new_job(env: Env) -> None:
    bad_url = "https://example.com/bad"
    ingestion = FakeIngestion(
        {bad_url: AppError(ErrorCode.FETCH_TIMEOUT, "抓取超时")}
    )
    service = build_service(env, ingestion)
    first = service.create_job([bad_url], enqueue=lambda _: None)
    service.run_job(first.job_id)

    enqueued: list[str] = []
    retried = service.retry_job(first.job_id, enqueue=enqueued.append)

    assert retried.job_id != first.job_id
    assert retried.accepted == 1
    assert enqueued == [retried.job_id]
    with pytest.raises(AppError) as exc_info:
        service.retry_job(retried.job_id, enqueue=enqueued.append)
    assert exc_info.value.code is ErrorCode.JOB_NOT_RETRYABLE


def test_recover_fails_running_and_requeues_queued(env: Env) -> None:
    service = build_service(env, FakeIngestion({}))
    result = service.create_job(
        ["https://example.com/a", "https://example.com/b"],
        enqueue=lambda _: None,
    )
    with env.session_factory() as session:
        job = session.get(IngestionJob, result.job_id)
        assert job is not None
        job.status = JobStatus.RUNNING.value
        items = list(
            session.query(IngestionJobItem)
            .filter_by(job_id=result.job_id)
            .order_by(IngestionJobItem.id)
        )
        items[0].status = ItemStatus.RUNNING.value
        session.commit()

    enqueued: list[str] = []
    recovered = service.recover(enqueue=enqueued.append)

    assert recovered == (result.job_id,)
    assert enqueued == [result.job_id]
    view = service.get_job(result.job_id)
    assert view.failed == 1
    assert view.status == JobStatus.QUEUED
    interrupted = next(
        item for item in view.items if item.error_code is not None
    )
    assert interrupted.error_code == ErrorCode.INGEST_INTERRUPTED.value


def test_idempotency_key_replays_existing_job(env: Env) -> None:
    service = build_service(env, FakeIngestion({}))
    first = service.create_job(
        ["https://example.com/a"],
        idempotency_key="same-key",
        enqueue=lambda _: None,
    )

    second = service.create_job(
        ["https://example.com/a"],
        idempotency_key="same-key",
        enqueue=lambda _: None,
    )

    assert second.job_id == first.job_id
    assert second.accepted == 0


def test_job_timeout_fails_queued_items(env: Env) -> None:
    service = build_service(
        env,
        FakeIngestion({}),
        settings=build_settings(ingestion_job_timeout_s=0),
    )
    result = service.create_job(
        ["https://example.com/a", "https://example.com/b"],
        enqueue=lambda _: None,
    )

    service.run_job(result.job_id)

    view = service.get_job(result.job_id)
    assert view.status == JobStatus.FAILED
    assert view.failed == 2
    assert all(
        item.error_code == ErrorCode.INGEST_TIMEOUT.value for item in view.items
    )
