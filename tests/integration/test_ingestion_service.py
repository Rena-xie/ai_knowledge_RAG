"""入库流水线（见《项目介绍.md》4.4、5.3、5.4）。"""

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select

from app.core.errors import AppError, ErrorCode
from app.core.settings import Settings
from app.infra.db import (
    ChildChunk,
    ParentChunk,
    create_db_engine,
    create_session_factory,
    init_db,
)
from app.infra.embedding import EmbeddingError
from app.infra.fetcher import FetchError, FetchResult
from app.infra.fts import FtsIndex
from app.infra.vector import VectorIndex
from app.services.ingestion import IngestionService
from app.services.source import SourceService

ARTICLE = """
<html><head><title>BM25 指南</title></head><body><article>
<h1>BM25 指南</h1>
<p>BM25 是一种稀疏检索算法，通过对词频和文档长度归一化来计算相关性得分。</p>
<h2>适用场景</h2>
<p>当查询包含专有名词或代码函数名时，BM25 往往比向量检索更准确。</p>
<p>混合检索使用倒数排名融合算法合并两路结果，兼顾语义召回与关键词召回。</p>
</article></body></html>
"""

NO_HEADING_ARTICLE = """
<html><head><title>教程页</title></head><body><article>
<p>Python 的数据类型包括整数、浮点数、字符串与布尔值等，
理解这些基本类型是学习编程语言的第一步。</p>
<p>整数在 Python 中没有大小限制，可以表示非常大的数值；
浮点数采用二进制存储，因此可能存在精度误差。</p>
<p>字符串可以使用单引号或双引号括起来，转义字符用反斜杠表示，也支持多行字符串与格式化写法。</p>
<p>布尔值只有真与假两个取值，通常由比较运算产生，并广泛用于条件判断与循环控制等流程之中。</p>
</article></body></html>
"""


class FakeFetcher:
    def __init__(self, result: FetchResult | Exception) -> None:
        self._result = result
        self.calls = 0

    def fetch(self, url: str) -> FetchResult:
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeEmbedder:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.texts: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise EmbeddingError("embedding 失败")
        self.texts = list(texts)
        return [[float(index + 1), 0.0, 0.0] for index, _ in enumerate(texts)]


def build_settings(**overrides) -> Settings:
    values: dict[str, object] = {
        "embedding_api_base": "https://example.invalid/v1",
        "embedding_api_key": "test",
        "llm_api_base": "https://example.invalid/v1",
        "llm_api_key": "test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, object]]:
    engine: Engine = create_db_engine(tmp_path / "app.db")
    init_db(engine)
    session_factory = create_session_factory(engine)
    fts = FtsIndex(engine)
    fts.ensure_schema()
    vector = VectorIndex(tmp_path / "chroma")
    source_service = SourceService(
        session_factory=session_factory, vector_index=vector, fts_index=fts
    )
    yield {
        "session_factory": session_factory,
        "fts": fts,
        "vector": vector,
        "source_service": source_service,
    }
    engine.dispose()


def build_service(
    env: dict[str, object],
    *,
    fetcher: FakeFetcher | None = None,
    embedder: FakeEmbedder | None = None,
    **settings_overrides,
) -> IngestionService:
    return IngestionService(
        session_factory=env["session_factory"],
        source_service=env["source_service"],
        fetcher=fetcher
        or FakeFetcher(
            FetchResult(
                url="https://example.com/a",
                content_type="text/html",
                html=ARTICLE,
            )
        ),
        embedder=embedder or FakeEmbedder(),
        fts_index=env["fts"],
        vector_index=env["vector"],
        settings=build_settings(**settings_overrides),
    )


def count_rows(env: dict[str, object], model: type) -> int:
    session_factory = env["session_factory"]
    with session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_ingest_completes_source_and_indexes_children(
    env: dict[str, object],
) -> None:
    service = build_service(env)

    result = service.ingest("https://example.com/a")

    assert result.triggered is True
    assert result.status == "completed"
    assert result.child_count >= 1
    assert count_rows(env, ChildChunk) == result.child_count
    assert count_rows(env, ParentChunk) >= 1
    fts: FtsIndex = env["fts"]
    vector: VectorIndex = env["vector"]
    assert fts.search("BM25", top_k=5)
    assert vector.count() == result.child_count

    source = env["source_service"].get(result.source_id)
    assert source.status == "completed"
    assert source.title == "BM25 指南"
    assert source.content_hash
    assert source.chunker_version == "v1"
    assert source.embedding_model == "BAAI/bge-m3"
    assert source.processed_at


def test_second_ingest_of_completed_source_is_noop(
    env: dict[str, object],
) -> None:
    service = build_service(env)
    first = service.ingest("https://example.com/a")

    second = service.ingest("https://example.com/a")

    assert second.triggered is False
    assert second.source_id == first.source_id
    assert count_rows(env, ChildChunk) == first.child_count


def test_fetch_failure_marks_failed_and_cleans_up(
    env: dict[str, object],
) -> None:
    fetcher = FakeFetcher(FetchError(ErrorCode.FETCH_TIMEOUT, "抓取超时"))
    service = build_service(env, fetcher=fetcher)

    with pytest.raises(AppError) as excinfo:
        service.ingest("https://example.com/a")

    assert excinfo.value.code == ErrorCode.FETCH_TIMEOUT
    assert count_rows(env, ChildChunk) == 0
    assert count_rows(env, ParentChunk) == 0
    source = env["source_service"].get(1)
    assert source.status == "failed"
    assert source.error_code == ErrorCode.FETCH_TIMEOUT.value
    assert env["vector"].count() == 0


def test_embedding_failure_compensates_partial_writes(
    env: dict[str, object],
) -> None:
    service = build_service(env, embedder=FakeEmbedder(fail=True))

    with pytest.raises(AppError) as excinfo:
        service.ingest("https://example.com/a")

    assert excinfo.value.code == ErrorCode.EMBEDDING_FAILED
    assert count_rows(env, ChildChunk) == 0
    assert count_rows(env, ParentChunk) == 0
    assert env["vector"].count() == 0
    assert env["fts"].search("BM25", top_k=5) == []
    source = env["source_service"].get(1)
    assert source.status == "failed"
    assert source.error_code == ErrorCode.EMBEDDING_FAILED.value


def test_failed_source_can_be_reingested(env: dict[str, object]) -> None:
    failing = build_service(
        env, fetcher=FakeFetcher(FetchError(ErrorCode.FETCH_HTTP_ERROR, "失败"))
    )
    with pytest.raises(AppError):
        failing.ingest("https://example.com/a")

    working = build_service(env)
    result = working.ingest("https://example.com/a")

    assert result.triggered is True
    assert result.status == "completed"


def test_ingest_busy_when_lock_is_held(env: dict[str, object]) -> None:
    service = build_service(env, ingest_lock_timeout_s=0)
    assert service._lock.acquire(blocking=False)  # noqa: SLF001 - 模拟并发入库
    try:
        with pytest.raises(AppError) as excinfo:
            service.ingest("https://example.com/a")
    finally:
        service._lock.release()  # noqa: SLF001

    assert excinfo.value.code == ErrorCode.INGEST_BUSY


def test_ingest_timeout_is_enforced(env: dict[str, object]) -> None:
    service = build_service(env, ingest_timeout_s=0)

    with pytest.raises(AppError) as excinfo:
        service.ingest("https://example.com/a")

    assert excinfo.value.code == ErrorCode.INGEST_TIMEOUT
    assert count_rows(env, ChildChunk) == 0
    source = env["source_service"].get(1)
    assert source.status == "failed"
    assert source.error_code == ErrorCode.INGEST_TIMEOUT.value


def test_ingest_uses_page_title_as_fallback_heading_path(
    env: dict[str, object],
) -> None:
    fetcher = FakeFetcher(
        FetchResult(
            url="https://example.com/a",
            content_type="text/html",
            html=NO_HEADING_ARTICLE,
        )
    )
    service = build_service(env, fetcher=fetcher)

    result = service.ingest("https://example.com/a")

    with env["session_factory"]() as session:
        children = session.scalars(select(ChildChunk)).all()
    assert result.status == "completed"
    assert children
    assert {child.heading_path for child in children} == {"教程页"}
