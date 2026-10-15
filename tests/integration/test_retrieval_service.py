"""检索服务：双路召回、RRF 与父级上下文（见《项目介绍.md》7.1–7.3）。"""

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import Engine

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
from app.infra.vector import VectorIndex
from app.services.retrieval import RetrievalService

NOW = "2026-10-09T00:00:00Z"


class FixedEmbedder:
    def __init__(self, vector: Sequence[float]) -> None:
        self._vector = list(vector)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(self._vector) for _ in texts]


def build_settings(**overrides) -> Settings:
    values: dict[str, object] = {
        "embedding_api_base": "https://example.invalid/v1",
        "embedding_api_key": "test",
        "llm_api_base": "https://example.invalid/v1",
        "llm_api_key": "test",
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


def build_service(env: dict[str, object], **overrides) -> RetrievalService:
    return RetrievalService(
        session_factory=env["session_factory"],
        embedder=FixedEmbedder([1.0, 0.0, 0.0]),
        fts_index=env["fts"],
        vector_index=env["vector"],
        settings=build_settings(**overrides),
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
