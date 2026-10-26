"""异步入库任务：job 状态机、逐条执行与恢复（v2 M1）。"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import utc_now_iso
from app.core.context import bind_job_id
from app.core.errors import AppError, ErrorCode
from app.core.settings import Settings
from app.domain.state import SourceStatus
from app.domain.url import InvalidUrlError, normalize_url, redact_url_credentials
from app.infra.db import IngestionJob, IngestionJobItem, KnowledgeSource
from app.services.ingestion import IngestionService

logger = logging.getLogger(__name__)


class JobStatus(StrEnum):
    """job 级状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ItemStatus(StrEnum):
    """单条 URL 状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.COMPLETED.value,
        JobStatus.PARTIAL.value,
        JobStatus.FAILED.value,
    }
)


@dataclass(frozen=True)
class SkippedItem:
    """创建 job 时被跳过的 URL。"""

    url: str
    reason: str
    source_id: int | None = None


@dataclass(frozen=True)
class JobCreateResult:
    """创建 job 的返回值。"""

    job_id: str
    status: str
    total: int
    accepted: int
    skipped: tuple[SkippedItem, ...]
    created_at: str


@dataclass(frozen=True)
class JobItemView:
    """job 内单条 URL 的对外视图。"""

    url: str
    canonical_url: str
    status: str
    source_id: int | None
    final_url: str | None
    duplicate_of_source_id: int | None
    changed: bool | None
    title: str | None
    skip_reason: str | None
    error_code: str | None
    error_message: str | None
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class JobView:
    """job 详情。"""

    job_id: str
    status: str
    total: int
    succeeded: int
    failed: int
    skipped: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    items: tuple[JobItemView, ...]
    items_truncated: bool


@dataclass(frozen=True)
class JobSummary:
    """job 列表项。"""

    job_id: str
    status: str
    total: int
    succeeded: int
    failed: int
    skipped: int
    created_at: str
    finished_at: str | None


class IngestionStats(TypedDict):
    window_hours: int
    total: int
    queued: int
    running: int
    completed: int
    partial: int
    failed: int
    average_duration_ms: float | None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class JobService:
    """管理 job 的创建、查询、执行与恢复。

    真正的抓取/切块/索引仍由 :class:`IngestionService` 完成；本类只负责任务编排。
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        ingestion_service: IngestionService,
        settings: Settings,
        clock: Callable[[], str] = utc_now_iso,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._ingestion_service = ingestion_service
        self._settings = settings
        self._clock = clock
        self._id_factory = id_factory or (lambda: f"job_{uuid.uuid4().hex}")

    def create_job(
        self,
        urls: Sequence[str],
        *,
        client_run_id: str | None = None,
        idempotency_key: str | None = None,
        force_refresh: bool = False,
        enqueue: Callable[[str], None],
    ) -> JobCreateResult:
        """创建 job；URL 去重、非法 URL 逐条记为 failed。"""

        if not urls:
            raise AppError(ErrorCode.INVALID_REQUEST, "urls 不能为空")
        if len(urls) > self._settings.ingestion_max_urls:
            raise AppError(
                ErrorCode.TOO_MANY_URLS,
                f"单次最多提交 {self._settings.ingestion_max_urls} 个 URL",
            )

        if idempotency_key:
            existing = self._find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._replay_result(existing)

        valid, invalid = self._normalize_urls(urls)
        if not valid and not invalid:
            raise AppError(ErrorCode.INVALID_REQUEST, "urls 中没有有效链接")
        existing_sources = self._existing_sources(
            [canonical for _, canonical in valid]
        )
        now = self._clock()
        job_id = self._id_factory()
        items: list[IngestionJobItem] = []
        skipped: list[SkippedItem] = []
        accepted = 0

        for raw, canonical in valid:
            source = existing_sources.get(canonical)
            if (
                source is not None
                and source.status == SourceStatus.COMPLETED.value
                and not force_refresh
            ):
                items.append(
                    self._make_item(
                        job_id=job_id,
                        url=raw,
                        canonical=canonical,
                        status=ItemStatus.SKIPPED,
                        now=now,
                        source_id=source.id,
                        title=source.title,
                        skip_reason="already_indexed",
                    )
                )
                skipped.append(
                    SkippedItem(
                        url=raw, reason="already_indexed", source_id=source.id
                    )
                )
                continue
            if source is not None and source.status in {
                SourceStatus.PENDING.value,
                SourceStatus.PROCESSING.value,
            }:
                items.append(
                    self._make_item(
                        job_id=job_id,
                        url=raw,
                        canonical=canonical,
                        status=ItemStatus.SKIPPED,
                        now=now,
                        source_id=source.id,
                        title=source.title,
                        skip_reason="already_processing",
                    )
                )
                skipped.append(
                    SkippedItem(
                        url=raw, reason="already_processing", source_id=source.id
                    )
                )
                continue
            items.append(
                self._make_item(
                    job_id=job_id,
                    url=raw,
                    canonical=canonical,
                    status=ItemStatus.QUEUED,
                    now=now,
                    force_refresh=force_refresh,
                )
            )
            accepted += 1

        for raw, message in invalid:
            items.append(
                self._make_item(
                    job_id=job_id,
                    url=raw,
                    canonical=raw,
                    status=ItemStatus.FAILED,
                    now=now,
                    error_code=ErrorCode.INVALID_REQUEST,
                    error_message=message,
                    finished=True,
                )
            )

        total = len(items)
        status = self._initial_status(accepted, items)
        job = IngestionJob(
            id=job_id,
            status=status.value,
            total_items=total,
            succeeded_items=0,
            failed_items=sum(
                1 for item in items if item.status == ItemStatus.FAILED.value
            ),
            skipped_items=sum(
                1 for item in items if item.status == ItemStatus.SKIPPED.value
            ),
            client_run_id=client_run_id,
            idempotency_key=idempotency_key,
            created_at=now,
            finished_at=now if status not in {JobStatus.QUEUED} else None,
        )
        try:
            with self._session_factory() as session:
                session.add(job)
                session.flush()
                session.add_all(items)
                session.commit()
        except IntegrityError:
            # 并发使用同一 Idempotency-Key 时，唯一约束兜底。
            if idempotency_key:
                existing = self._find_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return self._replay_result(existing)
            raise

        if accepted:
            try:
                enqueue(job_id)
            except AppError:
                self._mark_job_enqueue_failed(job_id)
                raise

        return JobCreateResult(
            job_id=job_id,
            status=status.value,
            total=total,
            accepted=accepted,
            skipped=tuple(skipped),
            created_at=now,
        )

    def get_job(self, job_id: str, *, item_limit: int = 50) -> JobView:
        """返回 job 详情；items 超过上限时截断。"""

        with self._session_factory() as session:
            job = session.get(IngestionJob, job_id)
            if job is None:
                raise AppError(ErrorCode.JOB_NOT_FOUND, f"任务不存在: {job_id}")
            total_items = int(
                session.scalar(
                    select(func.count())
                    .select_from(IngestionJobItem)
                    .where(IngestionJobItem.job_id == job_id)
                )
                or 0
            )
            rows = list(
                session.scalars(
                    select(IngestionJobItem)
                    .where(IngestionJobItem.job_id == job_id)
                    .order_by(IngestionJobItem.id)
                    .limit(item_limit)
                ).all()
            )
            return self._to_view(job, rows, items_truncated=total_items > len(rows))

    def list_jobs(
        self,
        *,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[JobSummary], int]:
        """分页查询 job 摘要。"""

        if page < 1 or size < 1:
            raise AppError(ErrorCode.INVALID_REQUEST, "page 与 size 必须为正整数")
        with self._session_factory() as session:
            condition = (
                [IngestionJob.status == status] if status is not None else []
            )
            total = int(
                session.scalar(
                    select(func.count()).select_from(IngestionJob).where(*condition)
                )
                or 0
            )
            rows = list(
                session.scalars(
                    select(IngestionJob)
                    .where(*condition)
                    .order_by(IngestionJob.created_at.desc())
                    .limit(size)
                    .offset((page - 1) * size)
                ).all()
            )
            return [self._to_summary(row) for row in rows], total

    def stats(self, *, since_hours: int = 24) -> IngestionStats:
        """返回近 N 小时的 job 统计与平均总耗时。"""

        cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
        with self._session_factory() as session:
            jobs = list(session.scalars(select(IngestionJob)).all())
        recent = [
            job
            for job in jobs
            if (created := _parse_iso(job.created_at)) is not None
            and created >= cutoff
        ]
        counts = {status.value: 0 for status in JobStatus}
        for job in recent:
            if job.status in counts:
                counts[job.status] += 1
        durations: list[float] = []
        for job in recent:
            if job.status not in TERMINAL_JOB_STATUSES:
                continue
            started = _parse_iso(job.started_at)
            finished = _parse_iso(job.finished_at)
            if started is not None and finished is not None:
                durations.append((finished - started).total_seconds() * 1000)
        return IngestionStats(
            window_hours=since_hours,
            total=len(recent),
            queued=counts.get("queued", 0),
            running=counts.get("running", 0),
            completed=counts.get("completed", 0),
            partial=counts.get("partial", 0),
            failed=counts.get("failed", 0),
            average_duration_ms=(
                round(sum(durations) / len(durations), 2)
                if durations
                else None
            ),
        )

    def retry_job(
        self, job_id: str, *, enqueue: Callable[[str], None]
    ) -> JobCreateResult:
        """对失败项创建新 job，不修改原 job 历史。"""

        with self._session_factory() as session:
            job = session.get(IngestionJob, job_id)
            if job is None:
                raise AppError(ErrorCode.JOB_NOT_FOUND, f"任务不存在: {job_id}")
            client_run_id = job.client_run_id
            failed_items = list(
                session.execute(
                    select(
                        IngestionJobItem.url, IngestionJobItem.force_refresh
                    ).where(
                        IngestionJobItem.job_id == job_id,
                        IngestionJobItem.status == ItemStatus.FAILED.value,
                    )
                    .order_by(IngestionJobItem.id)
                ).all()
            )
        failed_urls = [str(row[0]) for row in failed_items]
        if not failed_urls:
            raise AppError(ErrorCode.JOB_NOT_RETRYABLE, "该任务没有可重试的失败项")
        force_refresh = any(bool(row[1]) for row in failed_items)
        return self.create_job(
            failed_urls,
            client_run_id=client_run_id,
            force_refresh=force_refresh,
            enqueue=enqueue,
        )

    def recover(self, *, enqueue: Callable[[str], None]) -> tuple[str, ...]:
        """启动恢复：running item 标记失败，queued item 重新入队。"""

        now = self._clock()
        enqueue_ids: list[str] = []
        with self._session_factory() as session:
            running_items = list(
                session.scalars(
                    select(IngestionJobItem).where(
                        IngestionJobItem.status == ItemStatus.RUNNING.value
                    )
                ).all()
            )
            for item in running_items:
                item.status = ItemStatus.FAILED.value
                item.error_code = ErrorCode.INGEST_INTERRUPTED.value
                item.error_message = "服务重启导致入库中断，可重试失败项"
                item.finished_at = now

            active_jobs = list(
                session.scalars(
                    select(IngestionJob).where(
                        IngestionJob.status.in_(
                            [JobStatus.QUEUED.value, JobStatus.RUNNING.value]
                        )
                    )
                ).all()
            )
            for job in active_jobs:
                item_statuses = list(
                    session.scalars(
                        select(IngestionJobItem.status).where(
                            IngestionJobItem.job_id == job.id
                        )
                    ).all()
                )
                self._apply_counts(job, item_statuses, now)
                if ItemStatus.QUEUED.value in item_statuses:
                    job.status = JobStatus.QUEUED.value
                    job.finished_at = None
                    enqueue_ids.append(job.id)
            session.commit()

        for job_id in enqueue_ids:
            try:
                enqueue(job_id)
            except AppError:
                logger.warning("恢复 job 入队失败", extra={"job_id": job_id})
        return tuple(enqueue_ids)

    def run_job(
        self, job_id: str, *, should_stop: Callable[[], bool] | None = None
    ) -> None:
        """Worker 入口：串行执行 job 内所有 queued item。"""

        deadline = time.monotonic() + self._settings.ingestion_job_timeout_s
        with self._session_factory() as session:
            job = session.get(IngestionJob, job_id)
            if job is None:
                logger.warning("job 不存在，跳过", extra={"job_id": job_id})
                return
            if job.status not in {
                JobStatus.QUEUED.value,
                JobStatus.RUNNING.value,
            }:
                return
            job.status = JobStatus.RUNNING.value
            if job.started_at is None:
                job.started_at = self._clock()
            logger.info(
                "ingestion_job_started",
                extra={
                    "job_id": job_id,
                    "client_run_id": job.client_run_id,
                },
            )
            session.commit()

        while True:
            if should_stop is not None and should_stop():
                break
            if time.monotonic() > deadline:
                self._fail_queued(
                    job_id,
                    ErrorCode.INGEST_TIMEOUT,
                    "任务超过总时间预算",
                )
                break
            with self._session_factory() as session:
                item = session.scalars(
                    select(IngestionJobItem)
                    .where(
                        IngestionJobItem.job_id == job_id,
                        IngestionJobItem.status == ItemStatus.QUEUED.value,
                    )
                    .order_by(IngestionJobItem.id)
                    .limit(1)
                ).first()
                if item is None:
                    break
                item.status = ItemStatus.RUNNING.value
                item.attempts += 1
                item.started_at = self._clock()
                session.commit()
                item_id = item.id
                url = item.url
                force_refresh = bool(item.force_refresh)

            self._execute_item(
                job_id=job_id,
                item_id=item_id,
                url=url,
                force_refresh=force_refresh,
            )
            self._refresh_job(job_id)

        self._refresh_job(job_id)

    def _fail_queued(
        self, job_id: str, code: ErrorCode, message: str
    ) -> None:
        now = self._clock()
        with self._session_factory() as session:
            items = list(
                session.scalars(
                    select(IngestionJobItem).where(
                        IngestionJobItem.job_id == job_id,
                        IngestionJobItem.status == ItemStatus.QUEUED.value,
                    )
                ).all()
            )
            for item in items:
                item.status = ItemStatus.FAILED.value
                item.error_code = code.value
                item.error_message = message
                item.finished_at = now
            session.commit()

    def _execute_item(
        self,
        *,
        job_id: str,
        item_id: int,
        url: str,
        force_refresh: bool,
    ) -> None:
        try:
            with bind_job_id(job_id):
                result = self._ingestion_service.ingest(
                    url, force_refresh=force_refresh
                )
        except AppError as error:
            self._finish_item(
                job_id=job_id,
                item_id=item_id,
                status=ItemStatus.FAILED,
                error_code=error.code,
                error_message=error.message,
            )
            return
        except Exception as error:  # noqa: BLE001 - 逐条失败隔离
            logger.error(
                "job item 执行异常: %s",
                type(error).__name__,
                extra={"job_id": job_id, "item_id": item_id},
            )
            self._finish_item(
                job_id=job_id,
                item_id=item_id,
                status=ItemStatus.FAILED,
                error_code=ErrorCode.INTERNAL_ERROR,
                error_message="服务内部错误",
            )
            return

        if result.triggered:
            self._finish_item(
                job_id=job_id,
                item_id=item_id,
                status=ItemStatus.COMPLETED,
                source_id=result.source_id,
                final_url=result.final_url,
                changed=result.changed,
            )
            return

        reason = result.skip_reason or (
            "already_indexed"
            if result.status == SourceStatus.COMPLETED.value
            else "already_processing"
            if result.status in {
                SourceStatus.PENDING.value,
                SourceStatus.PROCESSING.value,
            }
            else "existing_source"
        )
        self._finish_item(
            job_id=job_id,
            item_id=item_id,
            status=ItemStatus.SKIPPED,
            source_id=result.source_id,
            skip_reason=reason,
            final_url=result.final_url,
            duplicate_of_source_id=result.duplicate_of_source_id,
            changed=False,
        )

    def _finish_item(
        self,
        *,
        job_id: str,
        item_id: int,
        status: ItemStatus,
        source_id: int | None = None,
        final_url: str | None = None,
        duplicate_of_source_id: int | None = None,
        changed: bool | None = None,
        error_code: ErrorCode | None = None,
        error_message: str | None = None,
        skip_reason: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            item = session.get(IngestionJobItem, item_id)
            if item is None or item.job_id != job_id:
                return
            item.status = status.value
            item.finished_at = self._clock()
            if source_id is not None:
                item.source_id = source_id
                source = session.get(KnowledgeSource, source_id)
                if source is not None:
                    item.title = source.title
            if final_url is not None:
                item.final_url = final_url
            if duplicate_of_source_id is not None:
                item.duplicate_of_source_id = duplicate_of_source_id
            if changed is not None:
                item.changed = changed
            if error_code is not None:
                item.error_code = error_code.value
            if error_message is not None:
                item.error_message = error_message
            if skip_reason is not None:
                item.skip_reason = skip_reason
                item.error_code = None
                item.error_message = None
            session.commit()

    def _refresh_job(self, job_id: str) -> None:
        now = self._clock()
        with self._session_factory() as session:
            job = session.get(IngestionJob, job_id)
            if job is None:
                return
            statuses = list(
                session.scalars(
                    select(IngestionJobItem.status).where(
                        IngestionJobItem.job_id == job_id
                    )
                ).all()
            )
            self._apply_counts(job, statuses, now)
            session.commit()

    @staticmethod
    def _apply_counts(
        job: IngestionJob, statuses: Sequence[str], now: str
    ) -> None:
        completed = statuses.count(ItemStatus.COMPLETED.value)
        failed = statuses.count(ItemStatus.FAILED.value)
        skipped = statuses.count(ItemStatus.SKIPPED.value)
        queued = statuses.count(ItemStatus.QUEUED.value)
        running = statuses.count(ItemStatus.RUNNING.value)
        job.succeeded_items = completed
        job.failed_items = failed
        job.skipped_items = skipped
        if queued or running:
            job.status = (
                JobStatus.RUNNING.value
                if running
                else JobStatus.QUEUED.value
            )
            return
        job.finished_at = now
        if failed == 0:
            job.status = JobStatus.COMPLETED.value
        elif completed == 0 and skipped == 0:
            job.status = JobStatus.FAILED.value
        else:
            job.status = JobStatus.PARTIAL.value

    def _normalize_urls(
        self, urls: Sequence[str]
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        valid: list[tuple[str, str]] = []
        invalid: list[tuple[str, str]] = []
        seen: set[str] = set()
        seen_invalid: set[str] = set()
        for raw in urls:
            text = (raw or "").strip()
            if not text or text in seen_invalid:
                continue
            try:
                canonical = normalize_url(text)
            except InvalidUrlError as error:
                seen_invalid.add(text)
                invalid.append((redact_url_credentials(text), str(error)))
                continue
            if canonical in seen:
                continue
            seen.add(canonical)
            valid.append((text, canonical))
        return valid, invalid

    def _existing_sources(
        self, canonical_urls: Sequence[str]
    ) -> dict[str, KnowledgeSource]:
        if not canonical_urls:
            return {}
        with self._session_factory() as session:
            rows = session.scalars(
                select(KnowledgeSource).where(
                    KnowledgeSource.canonical_url.in_(canonical_urls)
                )
            ).all()
            return {row.canonical_url: row for row in rows}

    def _find_by_idempotency_key(self, key: str) -> IngestionJob | None:
        with self._session_factory() as session:
            return session.scalar(
                select(IngestionJob).where(IngestionJob.idempotency_key == key)
            )

    def _replay_result(self, job: IngestionJob) -> JobCreateResult:
        return JobCreateResult(
            job_id=job.id,
            status=job.status,
            total=job.total_items,
            accepted=0,
            skipped=(),
            created_at=job.created_at,
        )

    def _mark_job_enqueue_failed(self, job_id: str) -> None:
        with self._session_factory() as session:
            job = session.get(IngestionJob, job_id)
            if job is None:
                return
            now = self._clock()
            queued_items = list(
                session.scalars(
                    select(IngestionJobItem).where(
                        IngestionJobItem.job_id == job_id,
                        IngestionJobItem.status == ItemStatus.QUEUED.value,
                    )
                ).all()
            )
            for item in queued_items:
                item.status = ItemStatus.FAILED.value
                item.error_code = ErrorCode.INGESTION_QUEUE_FULL.value
                item.error_message = "入库队列已满"
                item.finished_at = now
            job.status = JobStatus.FAILED.value
            job.error_code = ErrorCode.INGESTION_QUEUE_FULL.value
            job.failed_items = job.failed_items + len(queued_items)
            job.finished_at = now
            session.commit()

    @staticmethod
    def _initial_status(
        accepted: int, items: Sequence[IngestionJobItem]
    ) -> JobStatus:
        if accepted:
            return JobStatus.QUEUED
        if any(item.status == ItemStatus.FAILED.value for item in items):
            if any(item.status == ItemStatus.SKIPPED.value for item in items):
                return JobStatus.PARTIAL
            return JobStatus.FAILED
        return JobStatus.COMPLETED

    @staticmethod
    def _make_item(
        *,
        job_id: str,
        url: str,
        canonical: str,
        status: ItemStatus,
        now: str,
        source_id: int | None = None,
        title: str | None = None,
        error_code: ErrorCode | None = None,
        error_message: str | None = None,
        skip_reason: str | None = None,
        finished: bool = False,
        force_refresh: bool = False,
    ) -> IngestionJobItem:
        return IngestionJobItem(
            job_id=job_id,
            url=url,
            canonical_url=canonical,
            status=status.value,
            force_refresh=force_refresh,
            source_id=source_id,
            title=title,
            error_code=error_code.value if error_code else None,
            error_message=error_message,
            skip_reason=skip_reason,
            created_at=now,
            finished_at=now if finished else None,
        )

    @staticmethod
    def _to_view(
        job: IngestionJob,
        rows: Sequence[IngestionJobItem],
        *,
        items_truncated: bool,
    ) -> JobView:
        return JobView(
            job_id=job.id,
            status=job.status,
            total=job.total_items,
            succeeded=job.succeeded_items,
            failed=job.failed_items,
            skipped=job.skipped_items,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            items=tuple(
                JobItemView(
                    url=row.url,
                    canonical_url=row.canonical_url,
                    status=row.status,
                    source_id=row.source_id,
                    final_url=row.final_url,
                    duplicate_of_source_id=row.duplicate_of_source_id,
                    changed=row.changed,
                    title=row.title,
                    skip_reason=row.skip_reason,
                    error_code=row.error_code,
                    error_message=row.error_message,
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                )
                for row in rows
            ),
            items_truncated=items_truncated,
        )

    @staticmethod
    def _to_summary(job: IngestionJob) -> JobSummary:
        return JobSummary(
            job_id=job.id,
            status=job.status,
            total=job.total_items,
            succeeded=job.succeeded_items,
            failed=job.failed_items,
            skipped=job.skipped_items,
            created_at=job.created_at,
            finished_at=job.finished_at,
        )


class IngestionWorker:
    """单线程 FIFO Worker；同一时刻只处理一个 job/item。"""

    def __init__(self, service: JobService, *, queue_max: int) -> None:
        self._service = service
        self._queue: queue.Queue[str] = queue.Queue(maxsize=queue_max)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="ingestion-worker", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout_s: float = 65.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                logger.warning("入库 Worker 未在超时内退出")

    def submit(self, job_id: str) -> None:
        try:
            self._queue.put_nowait(job_id)
        except queue.Full as error:
            raise AppError(
                ErrorCode.INGESTION_QUEUE_FULL, "入库队列已满，请稍后再试"
            ) from error

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if not self._stop_event.is_set():
                    self._service.run_job(
                        job_id, should_stop=self._stop_event.is_set
                    )
            except Exception:  # noqa: BLE001 - Worker 不能因单个 job 崩溃
                logger.exception("入库 job 执行异常", extra={"job_id": job_id})
            finally:
                self._queue.task_done()
