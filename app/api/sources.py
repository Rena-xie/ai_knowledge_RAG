"""来源管理接口（见《项目开发说明_v1.md》8.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from app.api.deps import Container
from app.api.schemas import (
    IngestionCreateResponse,
    SkippedItemResponse,
    SourceContentResponse,
    SourceListResponse,
    SourceResponse,
    SubmitSourceRequest,
)
from app.core.errors import AppError, ErrorCode

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


def _container(request: Request) -> Container:
    return request.app.state.container


@router.post("", response_model=SourceResponse)
def submit_source(payload: SubmitSourceRequest, request: Request) -> SourceResponse:
    """提交 URL：同步完成入库并返回来源记录。"""

    container = _container(request)
    result = container.ingestion_service.ingest(payload.url)
    source = container.source_service.get(result.source_id)
    if source is None:
        raise AppError(ErrorCode.NOT_FOUND, "来源不存在")
    response = SourceResponse.model_validate(source)
    return response.model_copy(update={"triggered": result.triggered})


@router.get("", response_model=SourceListResponse)
def list_sources(
    request: Request,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> SourceListResponse:
    """分页查询来源列表。"""

    items, total = _container(request).source_service.list_sources(page=page, size=size)
    return SourceListResponse(
        items=[SourceResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        size=size,
    )


@router.get("/{source_id}", response_model=SourceResponse)
def get_source(source_id: int, request: Request) -> SourceResponse:
    """按 ID 查询来源。"""

    source = _container(request).source_service.get(source_id)
    if source is None:
        raise AppError(ErrorCode.NOT_FOUND, f"来源不存在: {source_id}")
    return SourceResponse.model_validate(source)


@router.get("/{source_id}/content", response_model=SourceContentResponse)
def get_source_content(
    source_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    size: int | None = Query(None, ge=1, le=100),
) -> SourceContentResponse:
    """分页查询来源元数据和 Parent/Child 正文树。"""

    container = _container(request)
    effective_size = (
        min(20, container.settings.content_preview_parent_limit)
        if size is None
        else size
    )
    content = container.source_inspection_service.get_content(
        source_id,
        page=page,
        size=effective_size,
    )
    return SourceContentResponse.model_validate(content)


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: int, request: Request) -> Response:
    """级联删除来源及其全部派生数据。"""

    _container(request).source_service.delete(source_id)
    return Response(status_code=204)


@router.post("/{source_id}/refresh", response_model=IngestionCreateResponse)
def refresh_source(
    source_id: int, request: Request, response: Response
) -> IngestionCreateResponse:
    """创建来源刷新任务；内容未变化时只更新 last_checked_at。"""

    container = _container(request)
    source = container.source_service.get(source_id)
    if source is None:
        raise AppError(ErrorCode.NOT_FOUND, f"来源不存在: {source_id}")
    result = container.job_service.create_job(
        [source.url],
        force_refresh=True,
        enqueue=container.ingestion_worker.submit,
    )
    response.status_code = 202 if result.accepted else 200
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
