"""任务统计接口（v2 M6）。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api.deps import Container
from app.api.schemas import IngestionStatsResponse

router = APIRouter(prefix="/api/v1/stats", tags=["stats"])


def _container(request: Request) -> Container:
    return request.app.state.container


@router.get("/ingestions", response_model=IngestionStatsResponse)
def ingestion_stats(
    request: Request,
    since_hours: int = Query(default=24, ge=1, le=720),
) -> IngestionStatsResponse:
    """返回近 N 小时的入库任务统计与当前队列深度。"""

    container = _container(request)
    stats = container.job_service.stats(since_hours=since_hours)
    return IngestionStatsResponse(
        **stats, queue_depth=container.ingestion_worker.pending
    )
