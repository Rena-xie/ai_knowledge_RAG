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
