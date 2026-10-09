"""FTS5 全文索引（见《项目介绍.md》5.1、7.1）。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.infra.db import create_db_engine, init_db
from app.infra.fts import FtsIndex


@pytest.fixture
def index(tmp_path: Path) -> Iterator[FtsIndex]:
    engine: Engine = create_db_engine(tmp_path / "app.db")
    init_db(engine)
    fts = FtsIndex(engine)
    fts.ensure_schema()
    yield fts
    engine.dispose()


def test_finds_chinese_terms_and_ranks_best_first(index: FtsIndex) -> None:
    index.upsert(1, "BM25 是稀疏检索算法，用于关键词匹配")
    index.upsert(2, "向量检索依赖语义相似度")

    # OR 召回语义：只命中「检索」的文档也会返回，但 BM25 排序把更相关的排前
    assert index.search("稀疏检索", top_k=5) == [1, 2]


def test_ranks_more_relevant_first(index: FtsIndex) -> None:
    index.upsert(1, "关键词 关键词 关键词")
    index.upsert(2, "关键词")

    assert index.search("关键词", top_k=5) == [1, 2]


def test_upsert_is_idempotent(index: FtsIndex) -> None:
    index.upsert(1, "重复写入内容")
    index.upsert(1, "重复写入内容")

    assert index.search("重复写入", top_k=5) == [1]


def test_delete_removes_document(index: FtsIndex) -> None:
    index.upsert(1, "待删除内容")
    index.delete(1)

    assert index.search("待删除", top_k=5) == []


def test_top_k_limits_results(index: FtsIndex) -> None:
    for child_id in range(1, 5):
        index.upsert(child_id, "共同关键词")

    assert len(index.search("共同关键词", top_k=2)) == 2


def test_empty_query_returns_nothing(index: FtsIndex) -> None:
    index.upsert(1, "任意内容")

    assert index.search("   ", top_k=5) == []
