"""异步入库任务接口（v2 M1）。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Query, Request, Response

from app.api.deps import Container
from app.api.schemas import (
    IngestionCreateRequest,
    IngestionCreateResponse,
    IngestionItemResponse,
    IngestionJobListResponse,
    IngestionJobResponse,
    IngestionJobSummaryResponse,
    SkippedItemResponse,
)
from app.services.ingestion_jobs import (
    TERMINAL_JOB_STATUSES,
    JobCreateResult,
    JobItemView,
    JobSummary,
    JobView,
)

router = APIRouter(prefix="/api/v1/ingestions", tags=["ingestions"])


def _container(request: Request) -> Container:
    return request.app.state.container


@router.post("", response_model=IngestionCreateResponse)
def create_ingestion(
    payload: IngestionCreateRequest,
    request: Request,
    response: Response,
) -> IngestionCreateResponse:
    """创建异步入库任务；有实际入库项时返回 202。"""

    container = _container(request)
    result = container.job_service.create_job(
        payload.urls,
        client_run_id=payload.client_run_id,
        idempotency_key=request.headers.get("Idempotency-Key"),
        enqueue=container.ingestion_worker.submit,
    )
    response.status_code = 202 if result.accepted else 200
    return _create_response(result)


@router.get("/{job_id}", response_model=IngestionJobResponse)
def get_ingestion(
    job_id: str,
    request: Request,
    wait_seconds: int = Query(default=0, ge=0, le=25),
) -> IngestionJobResponse:
    """查询任务；wait_seconds 用于可选的短轮询。"""

    container = _container(request)
    view = container.job_service.get_job(job_id)
    deadline = time.monotonic() + wait_seconds
    while (
        wait_seconds > 0
        and view.status not in TERMINAL_JOB_STATUSES
        and time.monotonic() < deadline
    ):
        time.sleep(0.2)
        view = container.job_service.get_job(job_id)
    return _job_response(view)


@router.get("", response_model=IngestionJobListResponse)
def list_ingestions(
    request: Request,
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> IngestionJobListResponse:
    """分页查询任务摘要。"""

    items, total = _container(request).job_service.list_jobs(
        status=status, page=page, size=size
    )
    return IngestionJobListResponse(
        items=[_summary_response(item) for item in items],
        total=total,
        page=page,
        size=size,
    )


@router.post("/{job_id}/retry", response_model=IngestionCreateResponse)
def retry_ingestion(
    job_id: str,
    request: Request,
    response: Response,
) -> IngestionCreateResponse:
    """对失败项创建新 job。"""

    container = _container(request)
    result = container.job_service.retry_job(
        job_id, enqueue=container.ingestion_worker.submit
    )
    response.status_code = 202 if result.accepted else 200
    return _create_response(result)


def _create_response(result: JobCreateResult) -> IngestionCreateResponse:
    return IngestionCreateResponse(
        job_id=result.job_id,
        status=result.status,
        total=result.total,
        accepted=result.accepted,
        skipped=[
            SkippedItemResponse(
                url=item.url, reason=item.reason, source_id=item.source_id
            )
            for item in result.skipped
        ],
        created_at=result.created_at,
    )


def _job_response(view: JobView) -> IngestionJobResponse:
    return IngestionJobResponse(
        job_id=view.job_id,
        status=view.status,
        total=view.total,
        succeeded=view.succeeded,
        failed=view.failed,
        skipped=view.skipped,
        created_at=view.created_at,
        started_at=view.started_at,
        finished_at=view.finished_at,
        items=[_item_response(item) for item in view.items],
        items_truncated=view.items_truncated,
    )


def _item_response(item: JobItemView) -> IngestionItemResponse:
    return IngestionItemResponse(
        url=item.url,
        canonical_url=item.canonical_url,
        status=item.status,
        source_id=item.source_id,
        title=item.title,
        error_code=item.error_code,
        error_message=item.error_message,
        started_at=item.started_at,
        finished_at=item.finished_at,
    )


def _summary_response(item: JobSummary) -> IngestionJobSummaryResponse:
    return IngestionJobSummaryResponse(
        job_id=item.job_id,
        status=item.status,
        total=item.total,
        succeeded=item.succeeded,
        failed=item.failed,
        skipped=item.skipped,
        created_at=item.created_at,
        finished_at=item.finished_at,
    )
