"""检索服务：双路召回 + RRF + 父级上下文恢复（见《项目开发说明_v1.md》7.1–7.3）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import Settings
from app.domain.rrf import FusedHit, rrf_fuse
from app.domain.state import SourceStatus
from app.infra.db import ChildChunk, KnowledgeSource, ParentChunk
from app.infra.embedding import EmbeddingClient
from app.infra.fts import FtsIndex
from app.infra.vector import VectorIndex

Row = tuple[ChildChunk, ParentChunk, KnowledgeSource]


@dataclass(frozen=True)
class Evidence:
    """一条可用于回答的证据（Parent 上下文 + 命中 Child 标识）。"""

    source_id: int
    child_id: int
    parent_id: int
    title: str | None
    url: str
    heading_path: str
    text: str
    score: float


@dataclass(frozen=True)
class RetrievalResult:
    """检索结果与调试用中间数据。"""

    evidences: tuple[Evidence, ...]
    dense_ids: tuple[int, ...]
    bm25_ids: tuple[int, ...]
    fused_ids: tuple[int, ...]
    max_similarity: float = 0.0


class RetrievalService:
    """执行双路召回、RRF 融合、父级去重与上下文预算截断。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        embedder: EmbeddingClient,
        fts_index: FtsIndex,
        vector_index: VectorIndex,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._fts_index = fts_index
        self._vector_index = vector_index
        self._settings = settings

    def search(self, question: str) -> RetrievalResult:
        embedding = self._embedder.embed([question])[0]
        dense_hits = self._vector_index.query(
            embedding, top_k=self._settings.dense_top_k
        )
        dense_ids = tuple(hit.child_id for hit in dense_hits)
        max_similarity = max(
            (hit.similarity for hit in dense_hits), default=0.0
        )
        bm25_ids = tuple(
            self._fts_index.search(question, top_k=self._settings.bm25_top_k)
        )
        fused = rrf_fuse(
            dense_ids,
            bm25_ids,
            k=self._settings.rrf_k,
            top_k=self._settings.final_top_k,
            head_reserve=self._settings.route_head_reserve,
        )
        fused_ids = tuple(hit.child_id for hit in fused)
        if not fused or max_similarity < self._settings.min_dense_similarity:
            # 相关性门槛：库内没有足够相关的证据时按证据不足处理（见 7.3 第 4 点）
            return RetrievalResult((), dense_ids, bm25_ids, fused_ids, max_similarity)
        rows = self._load_rows([hit.child_id for hit in fused])
        evidences = self._build_evidence(fused, rows)
        return RetrievalResult(
            evidences=tuple(evidences),
            dense_ids=dense_ids,
            bm25_ids=bm25_ids,
            fused_ids=fused_ids,
            max_similarity=max_similarity,
        )

    def _load_rows(self, child_ids: Sequence[int]) -> dict[int, Row]:
        with self._session_factory() as session:
            rows = session.execute(
                select(ChildChunk, ParentChunk, KnowledgeSource)
                .join(ParentChunk, ChildChunk.parent_id == ParentChunk.id)
                .join(KnowledgeSource, ChildChunk.source_id == KnowledgeSource.id)
                .where(
                    ChildChunk.id.in_(child_ids),
                    KnowledgeSource.status != SourceStatus.DELETING.value,
                )
            ).all()
            return {child.id: (child, parent, source) for child, parent, source in rows}

    def _build_evidence(
        self, fused: Sequence[FusedHit], rows: dict[int, Row]
    ) -> list[Evidence]:
        budget = self._settings.context_budget_chars
        used = 0
        seen_parents: set[int] = set()
        evidences: list[Evidence] = []
        for hit in fused:
            entry = rows.get(hit.child_id)
            if entry is None:
                continue
            child, parent, source = entry
            if parent.id in seen_parents:
                continue
            remaining = budget - used
            if remaining <= 0:
                break
            text = parent.content
            if len(text) > remaining:
                text = child.text[:remaining]
            seen_parents.add(parent.id)
            evidences.append(
                Evidence(
                    source_id=source.id,
                    child_id=child.id,
                    parent_id=parent.id,
                    title=source.title,
                    url=source.url,
                    heading_path=child.heading_path,
                    text=text,
                    score=hit.score,
                )
            )
            used += len(text)
        return evidences
