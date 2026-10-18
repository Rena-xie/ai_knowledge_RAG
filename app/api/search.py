"""检索调试接口（见《项目开发说明_v1.md》8.2）。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from app.api.schemas import EvidenceResponse, QuestionRequest, SearchResponse

router = APIRouter(prefix="/api/v1/search", tags=["search"])


@router.post("", response_model=SearchResponse)
def search(payload: QuestionRequest, request: Request) -> SearchResponse:
    """返回 Dense、BM25 与 RRF 融合结果及耗时。"""

    started = time.perf_counter()
    result = request.app.state.container.retrieval_service.search(payload.question)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return SearchResponse(
        question=payload.question,
        dense_ids=list(result.dense_ids),
        bm25_ids=list(result.bm25_ids),
        fused_ids=list(result.fused_ids),
        max_similarity=result.max_similarity,
        evidences=[
            EvidenceResponse(
                source_id=evidence.source_id,
                child_id=evidence.child_id,
                parent_id=evidence.parent_id,
                title=evidence.title,
                url=evidence.url,
                heading_path=evidence.heading_path,
                score=evidence.score,
                text=evidence.text,
            )
            for evidence in result.evidences
        ],
        elapsed_ms=elapsed_ms,
    )
