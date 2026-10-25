"""检索服务：双路召回、RRF 与父级上下文（见《项目开发说明_v1.md》7.1–7.3）。"""

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.core.errors import AppError, ErrorCode
from app.core.settings import Settings
from app.infra.db import (
    ChildChunk,
    KnowledgeSource,
    ParentChunk,
    create_db_engine,
    create_session_factory,
    init_db,
)
from app.infra.fts import FtsIndex
from app.infra.rerank import RerankError, RerankHit
from app.infra.vector import VectorIndex
from app.services.retrieval import RetrievalService

NOW = "2026-10-09T00:00:00Z"


class FixedEmbedder:
    def __init__(self, vector: Sequence[float]) -> None:
        self._vector = list(vector)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(self._vector) for _ in texts]


class FailingEmbedder:
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("embedding unavailable")


class FailingFts:
    def search(self, _question: str, *, top_k: int) -> list[int]:
        raise RuntimeError("fts unavailable")


class FakeRerank:
    def __init__(self, order: Sequence[int], *, fail: bool = False) -> None:
        self._order = list(order)
        self._fail = fail
        self.calls = 0

    def rerank(self, _query: str, documents: Sequence[str]) -> list[RerankHit]:
        self.calls += 1
        if self._fail:
            raise RerankError("rerank unavailable")
        return [
            RerankHit(index=index, score=1.0 - rank * 0.1)
            for rank, index in enumerate(self._order)
            if index < len(documents)
        ]

    def close(self) -> None:
        return None


def build_settings(**overrides) -> Settings:
    values: dict[str, object] = {
        "embedding_api_base": "https://example.invalid/v1",
        "embedding_api_key": "test",
        "dense_top_k": 5,
        "bm25_top_k": 5,
        "final_top_k": 3,
        "context_budget_chars": 200,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, object]]:
    engine: Engine = create_db_engine(tmp_path / "app.db")
    init_db(engine)
    fts = FtsIndex(engine)
    fts.ensure_schema()
    yield {
        "session_factory": create_session_factory(engine),
        "fts": fts,
        "vector": VectorIndex(tmp_path / "chroma"),
    }
    engine.dispose()


def seed_source(
    env: dict[str, object],
    *,
    url: str = "https://example.com/a",
    status: str = "completed",
    parent_content: str = "父级上下文内容",
    child_texts: Sequence[str] = ("BM25 稀疏检索", "向量检索"),
    embeddings: Sequence[Sequence[float]] = ([1.0, 0.0, 0.0], [0.5, 0.5, 0.0]),
) -> tuple[int, list[int]]:
    with env["session_factory"]() as session:
        source = KnowledgeSource(
            url=url,
            canonical_url=url,
            title="示例标题",
            status=status,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(source)
        session.flush()
        parent = ParentChunk(
            source_id=source.id, ordinal=0, heading_path="H", content=parent_content
        )
        session.add(parent)
        session.flush()
        child_ids: list[int] = []
        for index, text in enumerate(child_texts):
            child = ChildChunk(
                source_id=source.id,
                parent_id=parent.id,
                ordinal=index,
                text=text,
                embedding_text=text,
                content_type="paragraph",
                heading_path="H",
            )
            session.add(child)
            session.flush()
            child_ids.append(child.id)
        session.commit()
        source_id = source.id

    for child_id, text in zip(child_ids, child_texts, strict=True):
        env["fts"].upsert(child_id, text)
    env["vector"].upsert(
        child_ids,
        embeddings,
        [{"source_id": source_id} for _ in child_ids],
    )
    return source_id, child_ids


def build_service(
    env: dict[str, object],
    *,
    rerank_client: FakeRerank | None = None,
    embedder: object | None = None,
    fts: object | None = None,
    **overrides,
) -> RetrievalService:
    return RetrievalService(
        session_factory=env["session_factory"],
        embedder=embedder or FixedEmbedder([1.0, 0.0, 0.0]),
        fts_index=fts or env["fts"],
        vector_index=env["vector"],
        settings=build_settings(**overrides),
        rerank_client=rerank_client,
    )


def test_search_returns_evidence_with_parent_context(
    env: dict[str, object],
) -> None:
    source_id, child_ids = seed_source(env)
    service = build_service(env)

    result = service.search("BM25")

    assert result.evidences
    top = result.evidences[0]
    assert top.child_id == child_ids[0]
    assert top.source_id == source_id
    assert top.text == "父级上下文内容"
    assert top.title == "示例标题"
    assert top.score > 0
    assert child_ids[0] in result.dense_ids
    assert child_ids[0] in result.bm25_ids
    assert result.max_similarity == pytest.approx(1.0, abs=0.01)


def test_rerank_reorders_candidates_and_uses_its_score(
    env: dict[str, object],
) -> None:
    _, child_ids = seed_source(
        env,
        child_texts=("BM25 稀疏检索", "向量检索", "混合检索"),
        embeddings=(
            [1.0, 0.0, 0.0],
            [0.9, 0.1, 0.0],
            [0.8, 0.2, 0.0],
        ),
    )
    rerank = FakeRerank(order=[2, 0, 1])
    service = build_service(
        env,
        rerank_client=rerank,
        final_top_k=2,
        rerank_top_n=5,
    )

    result = service.search("检索")

    assert rerank.calls == 1
    assert result.rerank_applied is True
    assert result.rerank_model == "BAAI/bge-reranker-v2-m3"
    assert result.fused_ids[0] == child_ids[2]
    assert result.evidences[0].child_id == child_ids[2]
    assert result.evidences[0].score == pytest.approx(1.0)


def test_literal_match_is_promoted_after_rerank(env: dict[str, object]) -> None:
    """正文逐字包含代码片段的切片应排在 rerank 结果之前。"""

    _, child_ids = seed_source(
        env,
        child_texts=(">>> print('hello, world')", "向量检索", "混合检索"),
        embeddings=(
            [1.0, 0.0, 0.0],
            [0.9, 0.1, 0.0],
            [0.8, 0.2, 0.0],
        ),
    )
    rerank = FakeRerank(order=[1, 2, 0])
    service = build_service(
        env, rerank_client=rerank, final_top_k=2, rerank_top_n=5
    )

    result = service.search("print('hello, world') 出现在哪一章？")

    assert result.rerank_applied is True
    assert result.fused_ids[0] == child_ids[0]


def test_missing_literal_keeps_rerank_order(env: dict[str, object]) -> None:
    """候选都不包含该片段时，保持 rerank 原顺序。"""

    _, child_ids = seed_source(
        env,
        child_texts=("BM25 稀疏检索", "向量检索", "混合检索"),
        embeddings=(
            [1.0, 0.0, 0.0],
            [0.9, 0.1, 0.0],
            [0.8, 0.2, 0.0],
        ),
    )
    rerank = FakeRerank(order=[2, 1, 0])
    service = build_service(
        env, rerank_client=rerank, final_top_k=2, rerank_top_n=5
    )

    result = service.search("print('absent, literal') 在哪？")

    assert result.fused_ids[0] == child_ids[2]


def test_dense_failure_falls_back_to_bm25(env: dict[str, object]) -> None:
    _, child_ids = seed_source(env)
    service = build_service(env, embedder=FailingEmbedder())

    result = service.search("BM25")

    assert result.degraded_routes == ("dense",)
    assert result.evidences
    assert result.evidences[0].child_id == child_ids[0]


def test_bm25_failure_falls_back_to_dense(env: dict[str, object]) -> None:
    _, child_ids = seed_source(env)
    service = build_service(env, fts=FailingFts())

    result = service.search("BM25")

    assert result.degraded_routes == ("bm25",)
    assert result.evidences
    assert result.evidences[0].child_id == child_ids[0]


def test_both_routes_failing_raises(env: dict[str, object]) -> None:
    seed_source(env)
    service = build_service(
        env, embedder=FailingEmbedder(), fts=FailingFts()
    )

    with pytest.raises(AppError) as excinfo:
        service.search("BM25")

    assert excinfo.value.code is ErrorCode.RETRIEVAL_UNAVAILABLE


def test_rerank_failure_falls_back_to_rrf(env: dict[str, object]) -> None:
    seed_source(env)
    service = build_service(
        env,
        rerank_client=FakeRerank(order=[1, 0], fail=True),
        final_top_k=2,
        rerank_top_n=5,
    )

    result = service.search("BM25")

    assert result.rerank_applied is False
    assert result.evidences


def test_low_similarity_gate_returns_no_evidence(env: dict[str, object]) -> None:
    """最高 Dense 相似度低于门槛时按证据不足处理，不产出上下文。"""

    seed_source(env, embeddings=([0.0, 1.0, 0.0], [0.0, 0.5, 0.5]))
    service = build_service(env)

    result = service.search("BM25")

    assert result.evidences == ()
    assert result.fused_ids
    assert result.max_similarity < 0.4


def test_search_deduplicates_same_parent(env: dict[str, object]) -> None:
    _, child_ids = seed_source(env)
    service = build_service(env)

    result = service.search("检索")

    parent_ids = {evidence.parent_id for evidence in result.evidences}
    assert len(parent_ids) == len(result.evidences)
    assert set(child_ids)


def test_deleting_source_is_excluded(env: dict[str, object]) -> None:
    seed_source(env, status="deleting")
    service = build_service(env)

    result = service.search("BM25")

    assert result.evidences == ()
    assert result.dense_ids


@pytest.mark.parametrize("status", ["failed", "pending", "processing"])
def test_non_completed_source_is_excluded(
    env: dict[str, object], status: str
) -> None:
    """半成品来源即使索引中仍有残留，也不能进入问答证据。"""

    seed_source(env, status=status)
    service = build_service(env)

    result = service.search("BM25")

    assert result.evidences == ()
    assert result.dense_ids
    assert result.bm25_ids


def test_empty_retrieval_returns_no_evidence(env: dict[str, object]) -> None:
    service = build_service(env)

    result = service.search("任意问题")

    assert result.evidences == ()
    assert result.dense_ids == ()
    assert result.bm25_ids == ()


def test_context_budget_truncates_lower_ranked_evidence(
    env: dict[str, object],
) -> None:
    seed_source(env, parent_content="长内容" * 100)
    service = build_service(env, context_budget_chars=30)

    result = service.search("BM25")

    assert result.evidences
    assert all(len(evidence.text) <= 30 for evidence in result.evidences)
