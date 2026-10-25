"""入库流水线（见《项目开发说明_v1.md》4.4、5.3、5.4）。"""

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select

from app.core.errors import AppError, ErrorCode
from app.core.settings import Settings
from app.infra.db import (
    ChildChunk,
    KnowledgeSource,
    ParentChunk,
    SourceAlias,
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

NOW = "2026-10-09T00:00:00Z"

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
        self.budgets: list[float | None] = []

    def embed(
        self, texts: Sequence[str], *, budget_s: float | None = None
    ) -> list[list[float]]:
        self.calls += 1
        self.budgets.append(budget_s)
        if self.fail:
            raise EmbeddingError("embedding 失败")
        self.texts = list(texts)
        return [[float(index + 1), 0.0, 0.0] for index, _ in enumerate(texts)]


class MutableFetcher:
    """可切换 HTML 的抓取替身，用于刷新测试。"""

    def __init__(self, html: str, *, final_url: str | None = None) -> None:
        self.html = html
        self.final_url = final_url
        self.calls = 0

    def fetch(self, url: str) -> FetchResult:
        self.calls += 1
        final_url = self.final_url or url
        redirect_urls = (url,) if final_url != url else ()
        return FetchResult(
            url=final_url,
            content_type="text/html",
            html=self.html,
            redirect_urls=redirect_urls,
        )

    def close(self) -> None:
        return None


class MappingFetcher:
    """按提交 URL 返回不同最终 URL 的抓取替身。"""

    def __init__(self, responses: dict[str, tuple[str, str]]) -> None:
        self._responses = responses

    def fetch(self, url: str) -> FetchResult:
        final_url, html = self._responses[url]
        redirect_urls = (url,) if final_url != url else ()
        return FetchResult(
            url=final_url,
            content_type="text/html",
            html=html,
            redirect_urls=redirect_urls,
        )


def build_settings(**overrides) -> Settings:
    values: dict[str, object] = {
        "embedding_api_base": "https://example.invalid/v1",
        "embedding_api_key": "test",
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
    assert source.language_code == "zh"
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
    assert second.changed is False
    assert second.source_id == first.source_id
    assert count_rows(env, ChildChunk) == first.child_count


def test_redirect_sets_final_url_and_aliases(
    env: dict[str, object],
) -> None:
    fetcher = MappingFetcher(
        {
            "https://example.com/start": (
                "https://example.com/article",
                ARTICLE,
            )
        }
    )
    service = build_service(env, fetcher=fetcher)

    result = service.ingest("https://example.com/start")

    assert result.final_url == "https://example.com/article"
    source = env["source_service"].get(result.source_id)
    assert source.canonical_url == "https://example.com/article"
    assert source.identity_status == "confirmed"
    session_factory = env["session_factory"]
    with session_factory() as session:
        aliases = list(
            session.query(SourceAlias).filter_by(source_id=result.source_id).all()
        )
        assert {
            alias.normalized_url: alias.alias_type for alias in aliases
        } == {
            "https://example.com/start": "submitted",
            "https://example.com/article": "final",
        }


def test_duplicate_final_url_reuses_existing_source(
    env: dict[str, object],
) -> None:
    fetcher = FakeFetcher(
        FetchResult(
            url="https://example.com/article",
            content_type="text/html",
            html=ARTICLE,
        )
    )
    service = build_service(env, fetcher=fetcher)
    first = service.ingest("https://example.com/first")

    second = service.ingest("https://example.com/second")

    assert second.triggered is False
    assert second.source_id == first.source_id
    assert second.duplicate_of_source_id == first.source_id
    assert second.skip_reason == "duplicate_url"
    assert count_rows(env, KnowledgeSource) == 1
    assert count_rows(env, ChildChunk) == first.child_count


def test_duplicate_content_reuses_existing_source(
    env: dict[str, object],
) -> None:
    fetcher = MappingFetcher(
        {
            "https://example.com/a": ("https://example.com/a", ARTICLE),
            "https://example.com/b": ("https://example.com/b", ARTICLE),
        }
    )
    service = build_service(env, fetcher=fetcher)
    first = service.ingest("https://example.com/a")

    second = service.ingest("https://example.com/b")

    assert second.triggered is False
    assert second.source_id == first.source_id
    assert second.duplicate_of_source_id == first.source_id
    assert second.skip_reason == "duplicate_content"
    assert count_rows(env, KnowledgeSource) == 1
    assert count_rows(env, ChildChunk) == first.child_count


def test_amp_print_and_mirror_content_is_deduplicated(
    env: dict[str, object],
) -> None:
    fetcher = MappingFetcher(
        {
            "https://example.com/article": (
                "https://example.com/article",
                ARTICLE,
            ),
            "https://example.com/article/amp": (
                "https://example.com/article/amp",
                ARTICLE,
            ),
            "https://example.com/article/print": (
                "https://example.com/article/print",
                ARTICLE,
            ),
            "https://mirror.example/article": (
                "https://mirror.example/article",
                ARTICLE,
            ),
        }
    )
    service = build_service(env, fetcher=fetcher)
    first = service.ingest("https://example.com/article")

    duplicates = [
        service.ingest("https://example.com/article/amp"),
        service.ingest("https://example.com/article/print"),
        service.ingest("https://mirror.example/article"),
    ]

    assert all(item.source_id == first.source_id for item in duplicates)
    assert all(item.skip_reason == "duplicate_content" for item in duplicates)
    assert count_rows(env, KnowledgeSource) == 1
    assert count_rows(env, ChildChunk) == first.child_count


def test_same_final_url_with_different_content_is_conflict(
    env: dict[str, object],
) -> None:
    changed_html = ARTICLE.replace(
        "BM25 是一种稀疏检索算法", "完整不同的正文内容"
    )
    fetcher = MappingFetcher(
        {
            "https://example.com/a": (
                "https://example.com/article",
                ARTICLE,
            ),
            "https://example.com/b": (
                "https://example.com/article",
                changed_html,
            ),
        }
    )
    service = build_service(env, fetcher=fetcher)
    first = service.ingest("https://example.com/a")

    with pytest.raises(AppError) as excinfo:
        service.ingest("https://example.com/b")

    assert excinfo.value.code == ErrorCode.SOURCE_IDENTITY_CONFLICT
    existing = env["source_service"].get(first.source_id)
    assert existing.status == "completed"
    assert count_rows(env, KnowledgeSource) == 2
    assert count_rows(env, ChildChunk) == first.child_count


def test_refresh_content_conflict_preserves_old_source(
    env: dict[str, object],
) -> None:
    changed_html = ARTICLE.replace(
        "BM25 是一种稀疏检索算法", "完整不同的正文内容"
    )
    initial_fetcher = MappingFetcher(
        {
            "https://example.com/a": ("https://example.com/a", ARTICLE),
            "https://example.com/b": ("https://example.com/b", changed_html),
        }
    )
    service = build_service(env, fetcher=initial_fetcher)
    first = service.ingest("https://example.com/a")
    service.ingest("https://example.com/b")
    old_texts = child_texts(env, first.source_id)

    refresh_service = build_service(
        env,
        fetcher=MappingFetcher(
            {
                "https://example.com/a": (
                    "https://example.com/a",
                    changed_html,
                )
            }
        ),
    )
    with pytest.raises(AppError) as excinfo:
        refresh_service.ingest("https://example.com/a", force_refresh=True)

    assert excinfo.value.code == ErrorCode.SOURCE_IDENTITY_CONFLICT
    assert child_texts(env, first.source_id) == old_texts
    assert env["source_service"].get(first.source_id).status == "completed"


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


def test_lock_failure_marks_source_failed_for_retry(
    env: dict[str, object],
) -> None:
    """抢锁失败不留 pending 悬挂记录：标记 failed，重新提交可完成入库。"""

    service = build_service(env, ingest_lock_timeout_s=0)
    assert service._lock.acquire(blocking=False)  # noqa: SLF001 - 模拟并发入库
    try:
        with pytest.raises(AppError) as excinfo:
            service.ingest("https://example.com/a")
    finally:
        service._lock.release()  # noqa: SLF001

    assert excinfo.value.code == ErrorCode.INGEST_BUSY
    blocked = env["source_service"].get(1)
    assert blocked.status == "failed"
    assert blocked.error_code == ErrorCode.INGEST_BUSY.value

    retried = build_service(env).ingest("https://example.com/a")

    assert retried.triggered is True
    assert retried.status == "completed"


def test_recover_interrupted_sources_marks_them_failed(
    env: dict[str, object],
) -> None:
    """启动恢复：遗留的 pending/processing 记录标记 failed 后可重新提交。"""

    session_factory = env["session_factory"]
    with session_factory() as session:
        for status in ("pending", "processing", "completed"):
            session.add(
                KnowledgeSource(
                    url=f"https://example.com/{status}",
                    canonical_url=f"https://example.com/{status}",
                    status=status,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.commit()

    recovered = build_service(env).recover_interrupted()

    assert len(recovered) == 2
    source_service = env["source_service"]
    for source_id in (1, 2):
        source = source_service.get(source_id)
        assert source.status == "failed"
        assert source.error_code == ErrorCode.INGEST_INTERRUPTED.value
    assert source_service.get(3).status == "completed"

    retried = build_service(env).ingest("https://example.com/pending")

    assert retried.triggered is True
    assert retried.status == "completed"


def test_recover_interrupted_cleans_partial_chunks_and_indexes(
    env: dict[str, object],
) -> None:
    """模拟 chunks 已提交后崩溃：重启恢复必须清掉半成品数据。"""

    service = build_service(env)
    completed = service.ingest("https://example.com/a")
    assert completed.child_count > 0

    with env["session_factory"]() as session:
        source = session.get(KnowledgeSource, completed.source_id)
        assert source is not None
        source.status = "processing"
        session.commit()

    recovered = build_service(env).recover_interrupted()

    assert recovered == (completed.source_id,)
    assert count_rows(env, ChildChunk) == 0
    assert count_rows(env, ParentChunk) == 0
    assert env["vector"].count() == 0
    assert env["fts"].all_ids() == []
    source = env["source_service"].get(completed.source_id)
    assert source.status == "failed"
    assert source.error_code == ErrorCode.INGEST_INTERRUPTED.value

    retried = build_service(env).ingest("https://example.com/a")
    assert retried.triggered is True
    assert retried.status == "completed"
    assert count_rows(env, ChildChunk) == retried.child_count
    assert env["vector"].count() == retried.child_count
    assert env["fts"].all_ids()


def test_ingest_passes_remaining_budget_to_embedder(
    env: dict[str, object],
) -> None:
    embedder = FakeEmbedder()
    service = build_service(env, embedder=embedder, ingest_timeout_s=30)

    service.ingest("https://example.com/a")

    assert embedder.budgets
    budget = embedder.budgets[0]
    assert budget is not None
    assert 0 < budget <= 30


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


def child_texts(env: dict[str, object], source_id: int) -> list[str]:
    with env["session_factory"]() as session:
        return list(
            session.scalars(
                select(ChildChunk.text).where(
                    ChildChunk.source_id == source_id
                )
            ).all()
        )


def child_ids(env: dict[str, object], source_id: int) -> set[int]:
    with env["session_factory"]() as session:
        return set(
            session.scalars(
                select(ChildChunk.id).where(
                    ChildChunk.source_id == source_id
                )
            ).all()
        )


def test_refresh_replaces_content_and_indexes(env: dict[str, object]) -> None:
    fetcher = MutableFetcher(ARTICLE)
    embedder = FakeEmbedder()
    service = build_service(env, fetcher=fetcher, embedder=embedder)
    first = service.ingest("https://example.com/a")
    old_ids = child_ids(env, first.source_id)
    old_hash = env["source_service"].get(first.source_id).content_hash

    fetcher.html = ARTICLE.replace(
        "BM25 是一种稀疏检索算法", "BM25 是一种新的稀疏检索算法"
    )
    refreshed = service.ingest(
        "https://example.com/a", force_refresh=True
    )

    assert refreshed.changed is True
    assert refreshed.child_count == count_rows(env, ChildChunk)
    assert embedder.calls == 2
    new_ids = child_ids(env, first.source_id)
    assert old_ids.isdisjoint(new_ids)
    assert any(
        "新的稀疏检索算法" in text for text in child_texts(env, first.source_id)
    )
    source = env["source_service"].get(first.source_id)
    assert source.status == "completed"
    assert source.content_hash != old_hash
    assert source.last_checked_at is not None
    assert env["vector"].count() == refreshed.child_count


def test_refresh_embedding_failure_keeps_old_content(
    env: dict[str, object],
) -> None:
    fetcher = MutableFetcher(ARTICLE)
    embedder = FakeEmbedder()
    service = build_service(env, fetcher=fetcher, embedder=embedder)
    first = service.ingest("https://example.com/a")
    old_texts = child_texts(env, first.source_id)
    old_count = count_rows(env, ChildChunk)

    fetcher.html = ARTICLE.replace(
        "BM25 是一种稀疏检索算法", "BM25 是一种新的稀疏检索算法"
    )
    embedder.fail = True
    with pytest.raises(AppError) as excinfo:
        service.ingest("https://example.com/a", force_refresh=True)

    assert excinfo.value.code == ErrorCode.EMBEDDING_FAILED
    assert child_texts(env, first.source_id) == old_texts
    assert count_rows(env, ChildChunk) == old_count
    assert env["vector"].count() == old_count
    assert env["source_service"].get(first.source_id).status == "completed"


def test_refresh_unchanged_content_skips_embedding(
    env: dict[str, object],
) -> None:
    fetcher = MutableFetcher(ARTICLE)
    embedder = FakeEmbedder()
    service = build_service(env, fetcher=fetcher, embedder=embedder)
    first = service.ingest("https://example.com/a")
    old_texts = child_texts(env, first.source_id)

    refreshed = service.ingest(
        "https://example.com/a", force_refresh=True
    )

    assert refreshed.changed is False
    assert embedder.calls == 1
    assert child_texts(env, first.source_id) == old_texts
    source = env["source_service"].get(first.source_id)
    assert source.status == "completed"
    assert source.last_checked_at is not None


def test_refresh_updates_final_url_without_changing_source_id(
    env: dict[str, object],
) -> None:
    fetcher = MutableFetcher(ARTICLE)
    service = build_service(env, fetcher=fetcher)
    first = service.ingest("https://example.com/a")

    fetcher.final_url = "https://cdn.example.com/article"
    refreshed = service.ingest(
        "https://example.com/a", force_refresh=True
    )

    assert refreshed.source_id == first.source_id
    assert refreshed.final_url == "https://cdn.example.com/article"
    source = env["source_service"].get(first.source_id)
    assert source.canonical_url == "https://cdn.example.com/article"
    assert source.identity_status == "confirmed"


def test_refresh_index_failure_rolls_back_old_data(
    env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = MutableFetcher(ARTICLE)
    service = build_service(env, fetcher=fetcher)
    first = service.ingest("https://example.com/a")
    old_texts = child_texts(env, first.source_id)
    old_ids = child_ids(env, first.source_id)
    old_count = env["vector"].count()

    fetcher.html = ARTICLE.replace(
        "BM25 是一种稀疏检索算法", "BM25 是一种新的稀疏检索算法"
    )

    def fail_upsert(*args: object, **kwargs: object) -> None:
        raise RuntimeError("vector unavailable")

    monkeypatch.setattr(env["vector"], "upsert", fail_upsert)
    with pytest.raises(AppError):
        service.ingest("https://example.com/a", force_refresh=True)

    assert child_texts(env, first.source_id) == old_texts
    assert child_ids(env, first.source_id) == old_ids
    assert env["vector"].count() == old_count
    assert env["source_service"].get(first.source_id).status == "completed"
    assert set(env["fts"].search("BM25", top_k=10)) & old_ids
