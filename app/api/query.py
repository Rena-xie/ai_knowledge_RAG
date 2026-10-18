"""问答接口（见《项目开发说明_v1.md》8.2、8.3）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.schemas import (
    CitationResponse,
    QueryResponse,
    QuestionRequest,
    UsageResponse,
)

router = APIRouter(prefix="/api/v1/query", tags=["query"])


@router.post("", response_model=QueryResponse)
def query(payload: QuestionRequest, request: Request) -> QueryResponse:
    """返回答案、结构化引用与引用校验标记。"""

    result = request.app.state.container.qa_service.answer(payload.question)
    return QueryResponse(
        answer=result.text,
        answer_type=result.answer_type,
        citations=[
            CitationResponse(
                source_id=citation.source_id,
                child_id=citation.child_id,
                title=citation.title,
                url=citation.url,
                heading_path=citation.heading_path,
            )
            for citation in result.citations
        ],
        citations_validated=result.citations_validated,
        usage=UsageResponse(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        ),
    )
