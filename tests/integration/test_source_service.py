"""来源管理与级联删除（见《项目介绍.md》5.3、5.5）。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
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
from app.services.source import SourceService

NOW = "2026-10-09T00:00:00Z"


class FailingOnceVector:
    """首次删除抛错，用于验证级联删除可重入。"""

    def __init__(self, inner: VectorIndex) -> None:
        self._inner = inner
        self.should_fail = True

    def delete_by_source(self, source_id: int) -> None:
        if self.should_fail:
            self.should_fail = False
            raise RuntimeError("vector 删除失败")
        self._inner.delete_by_source(source_id)


class AppErrorVector:
    """删除时抛出带错误码的业务异常，用于验证错误码原样保留。"""

    def delete_by_source(self, source_id: int) -> None:
        raise AppError(ErrorCode.INDEX_WRITE_FAILED, "chroma 写入失败")


class ExecuteFailingSession:
    """代理真实 Session，仅让 execute 失败，用于模拟数据库故障。"""

    def __init__(self, inner: Session) -> None:
        self._inner = inner

    def __enter__(self) -> "ExecuteFailingSession":
        return self

    def __exit__(self, *_exc_info: object) -> bool:
        self._inner.close()
        return False

    def get(self, *args: object, **kwargs: object):
        return self._inner.get(*args, **kwargs)

    def scalars(self, *args: object, **kwargs: object):
        return self._inner.scalars(*args, **kwargs)

    def commit(self) -> None:
        self._inner.commit()

    def execute(self, *args: object, **kwargs: object):
        raise OperationalError(
            "DELETE FROM knowledge_sources", {}, Exception("database is locked")
        )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, object]]:
    engine: Engine = create_db_engine(tmp_path / "app.db")
    init_db(engine)
    session_factory = create_session_factory(engine)
    fts = FtsIndex(engine)
    fts.ensure_schema()
    vector = VectorIndex(tmp_path / "chroma")
    service = SourceService(
        session_factory=session_factory, vector_index=vector, fts_index=fts
    )
    yield {
        "engine": engine,
        "session_factory": session_factory,
        "fts": fts,
        "vector": vector,
        "service": service,
        "tmp_path": tmp_path,
    }
    engine.dispose()


def seed_source(
    env: dict[str, object],
    *,
    url: str = "https://example.com/a",
    status: str = "completed",
    children: int = 2,
) -> int:
    session_factory = env["session_factory"]
    with session_factory() as session:
        source = KnowledgeSource(
            url=url,
            canonical_url=url,
            status=status,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(source)
        session.flush()
        parent = ParentChunk(
            source_id=source.id, ordinal=0, heading_path="A", content="父级内容"
        )
        session.add(parent)
        session.flush()
        child_ids = []
        for index in range(children):
            child = ChildChunk(
                source_id=source.id,
                parent_id=parent.id,
                ordinal=index,
                text=f"切片{index}",
                embedding_text=f"A 切片{index}",
                content_type="paragraph",
                heading_path="A",
            )
            session.add(child)
            session.flush()
            child_ids.append(child.id)
        session.commit()
        source_id = source.id

    fts: FtsIndex = env["fts"]
    vector: VectorIndex = env["vector"]
    if not child_ids:
        return source_id
    for child_id in child_ids:
        fts.upsert(child_id, f"A 切片{child_id}")
    vector.upsert(
        child_ids=child_ids,
        embeddings=[[1.0, 0.0, 0.0] for _ in child_ids],
        metadatas=[{"source_id": source_id} for _ in child_ids],
    )
    return source_id


def count_rows(env: dict[str, object], model: type) -> int:
    session_factory = env["session_factory"]
    with session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_creates_source_with_normalized_url(env: dict[str, object]) -> None:
    service: SourceService = env["service"]

    result = service.get_or_create("HTTPS://Example.com/a?utm_source=x&id=7#top")

    assert result.triggered is True
    assert result.source.status == "pending"
    assert result.source.canonical_url == "https://example.com/a?id=7"


def test_duplicate_completed_source_is_not_retriggered(
    env: dict[str, object],
) -> None:
    service: SourceService = env["service"]
    source_id = seed_source(env, status="completed")

    result = service.get_or_create("https://example.com/a")

    assert result.triggered is False
    assert result.source.id == source_id
    assert count_rows(env, KnowledgeSource) == 1


def test_duplicate_failed_source_is_retriggered(env: dict[str, object]) -> None:
    service: SourceService = env["service"]
    seed_source(env, status="failed")

    result = service.get_or_create("https://example.com/a")

    assert result.triggered is True
    assert result.source.status == "failed"


def test_get_or_create_recovers_when_concurrent_insert_wins(
    env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发提交同一新 URL：唯一约束冲突时回查既有记录，不抛异常。"""

    service: SourceService = env["service"]
    created = service.get_or_create("https://example.com/a")

    original = Session.scalar
    calls = {"count": 0}

    def flaky_scalar(self, statement, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return None  # 模拟并发窗口：本次查不到，插入时记录已存在
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", flaky_scalar)

    duplicate = service.get_or_create("https://example.com/a")

    assert duplicate.source.id == created.source.id
    assert duplicate.triggered is False
    assert count_rows(env, KnowledgeSource) == 1


def test_list_sources_paginates(env: dict[str, object]) -> None:
    service: SourceService = env["service"]
    for index in range(3):
        seed_source(env, url=f"https://example.com/{index}")

    items, total = service.list_sources(page=1, size=2)

    assert total == 3
    assert len(items) == 2


def test_get_returns_none_for_missing_source(env: dict[str, object]) -> None:
    service: SourceService = env["service"]

    assert service.get(999) is None


def test_get_or_create_rejects_invalid_url(env: dict[str, object]) -> None:
    service: SourceService = env["service"]

    with pytest.raises(AppError) as excinfo:
        service.get_or_create("ftp://example.com/a")

    assert excinfo.value.code == ErrorCode.INVALID_REQUEST


def test_delete_removes_all_traces(env: dict[str, object]) -> None:
    service: SourceService = env["service"]
    fts: FtsIndex = env["fts"]
    vector: VectorIndex = env["vector"]
    source_id = seed_source(env)
    assert vector.count() == 2

    service.delete(source_id)

    assert service.get(source_id) is None
    assert count_rows(env, ParentChunk) == 0
    assert count_rows(env, ChildChunk) == 0
    assert vector.count() == 0
    assert fts.search("切片", top_k=5) == []


def test_delete_missing_source_raises_not_found(env: dict[str, object]) -> None:
    service: SourceService = env["service"]

    with pytest.raises(AppError) as excinfo:
        service.delete(999)

    assert excinfo.value.code == ErrorCode.NOT_FOUND


def test_delete_pending_source_removes_row_directly(env: dict[str, object]) -> None:
    service: SourceService = env["service"]
    source_id = seed_source(env, status="pending", children=0)

    service.delete(source_id)

    assert service.get(source_id) is None


def test_delete_is_reentrant_after_failure(env: dict[str, object]) -> None:
    source_id = seed_source(env)
    failing_vector = FailingOnceVector(env["vector"])
    service = SourceService(
        session_factory=env["session_factory"],
        vector_index=failing_vector,
        fts_index=env["fts"],
    )

    with pytest.raises(AppError) as excinfo:
        service.delete(source_id)

    assert excinfo.value.code == ErrorCode.INDEX_WRITE_FAILED
    source = service.get(source_id)
    assert source is not None
    assert source.status == "deleting"
    assert source.error_code == ErrorCode.INDEX_WRITE_FAILED.value

    service.delete(source_id)

    assert service.get(source_id) is None


def test_delete_app_error_keeps_index_code(env: dict[str, object]) -> None:
    """索引层已给出错误码时原样记录，不再被改写成数据库故障。"""

    source_id = seed_source(env)
    service = SourceService(
        session_factory=env["session_factory"],
        vector_index=AppErrorVector(),
        fts_index=env["fts"],
    )

    with pytest.raises(AppError) as excinfo:
        service.delete(source_id)

    assert excinfo.value.code == ErrorCode.INDEX_WRITE_FAILED
    source = service.get(source_id)
    assert source is not None
    assert source.error_code == ErrorCode.INDEX_WRITE_FAILED.value


def test_delete_db_failure_records_db_error(env: dict[str, object]) -> None:
    """关系数据删除失败应记录为数据库故障，而不是索引写入失败。"""

    source_id = seed_source(env)
    real_factory = env["session_factory"]

    def failing_factory() -> ExecuteFailingSession:
        return ExecuteFailingSession(real_factory())

    service = SourceService(
        session_factory=failing_factory,
        vector_index=env["vector"],
        fts_index=env["fts"],
    )

    with pytest.raises(AppError) as excinfo:
        service.delete(source_id)

    assert excinfo.value.code == ErrorCode.DB_ERROR
    source = service.get(source_id)
    assert source is not None
    assert source.status == "deleting"
    assert source.error_code == ErrorCode.DB_ERROR.value
