"""API 请求与响应模型（见《项目介绍.md》8.1–8.3）。"""

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
    elapsed_ms: float


class CitationResponse(BaseModel):
    """通过校验的引用。"""

    source_id: int
    child_id: int
    title: str | None
    url: str
    heading_path: str


class UsageResponse(BaseModel):
    """Token 用量。"""

    prompt_tokens: int
    completion_tokens: int


class QueryResponse(BaseModel):
    """问答响应。"""

    answer: str
    answer_type: str
    citations: list[CitationResponse]
    citations_validated: bool
    usage: UsageResponse
