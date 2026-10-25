"""API 请求与响应模型（见《项目开发说明_v1.md》8.1–8.3）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SubmitSourceRequest(BaseModel):
    """提交来源请求。"""

    url: str = Field(min_length=1, max_length=2048)


class SourceResponse(BaseModel):
    """来源记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    canonical_url: str
    title: str | None
    status: str
    language_code: str | None
    identity_status: str
    identity_verified_at: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    processed_at: str | None
    triggered: bool | None = None


class SourceListResponse(BaseModel):
    """来源分页响应。"""

    items: list[SourceResponse]
    total: int
    page: int
    size: int


class QuestionRequest(BaseModel):
    """检索与问答的公共请求体。"""

    question: str = Field(min_length=1, max_length=1000)


class EvidenceResponse(BaseModel):
    """检索命中的证据。"""

    source_id: int
    child_id: int
    parent_id: int
    title: str | None
    url: str
    heading_path: str
    score: float
    text: str


class SearchResponse(BaseModel):
    """调试检索响应。"""

    question: str
    dense_ids: list[int]
    bm25_ids: list[int]
    fused_ids: list[int]
    evidences: list[EvidenceResponse]
    max_similarity: float
    rerank_applied: bool = False
    rerank_model: str | None = None
    elapsed_ms: float


class IngestionCreateRequest(BaseModel):
    """创建异步入库任务。"""

    urls: list[str] = Field(min_length=1)
    client_run_id: str | None = Field(default=None, max_length=128)
    force_refresh: bool = False


class SkippedItemResponse(BaseModel):
    """创建任务时被跳过的 URL。"""

    url: str
    reason: str
    source_id: int | None = None


class IngestionCreateResponse(BaseModel):
    """创建任务响应。"""

    job_id: str
    status: str
    total: int
    accepted: int
    skipped: list[SkippedItemResponse]
    created_at: str


class IngestionItemResponse(BaseModel):
    """任务内单条 URL 状态。"""

    url: str
    canonical_url: str
    final_url: str | None = None
    status: str
    source_id: int | None = None
    duplicate_of_source_id: int | None = None
    changed: bool | None = None
    title: str | None = None
    skip_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class IngestionJobResponse(BaseModel):
    """任务详情。"""

    job_id: str
    status: str
    total: int
    succeeded: int
    failed: int
    skipped: int
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    items: list[IngestionItemResponse]
    items_truncated: bool = False


class IngestionJobSummaryResponse(BaseModel):
    """任务列表项。"""

    job_id: str
    status: str
    total: int
    succeeded: int
    failed: int
    skipped: int
    created_at: str
    finished_at: str | None = None


class IngestionJobListResponse(BaseModel):
    """任务分页列表。"""

    items: list[IngestionJobSummaryResponse]
    total: int
    page: int
    size: int


class IngestionStatsResponse(BaseModel):
    """异步入库统计。"""

    window_hours: int
    total: int
    queued: int
    running: int
    completed: int
    partial: int
    failed: int
    average_duration_ms: float | None = None
    queue_depth: int = 0
