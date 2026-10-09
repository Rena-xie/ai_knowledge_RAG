"""FTS5 全文索引（见《项目介绍.md》5.1、7.1）。"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import jieba
from sqlalchemy import Engine, text

jieba.setLogLevel(logging.WARNING)

CREATE_FTS_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS child_chunks_fts "
    "USING fts5(child_id UNINDEXED, embedding_text)"
)


def tokenize(value: str) -> str:
    """用 jieba 搜索引擎模式分词，返回空格分隔的词串。"""

    return " ".join(
        token.strip() for token in jieba.cut_for_search(value) if token.strip()
    )


def _quote(token: str) -> str:
    return f'"{token.replace(chr(34), chr(34) * 2)}"'


class FtsIndex:
    """child_chunks_fts 的建表、写入、删除与 BM25 检索。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        with self._engine.begin() as connection:
            connection.execute(text(CREATE_FTS_SQL))

    def reset(self) -> None:
        """清空全文索引并重建虚表（用于全量重建）。"""

        with self._engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS child_chunks_fts"))
        self.ensure_schema()

    def all_ids(self) -> list[int]:
        """返回索引中的全部 child_id。"""

        with self._engine.connect() as connection:
            rows = connection.execute(text("SELECT child_id FROM child_chunks_fts"))
            return [int(row[0]) for row in rows]

    def upsert(self, child_id: int, embedding_text: str) -> None:
        tokens = tokenize(embedding_text)
        with self._engine.begin() as connection:
            connection.execute(
                text("DELETE FROM child_chunks_fts WHERE child_id = :child_id"),
                {"child_id": child_id},
            )
            connection.execute(
                text(
                    "INSERT INTO child_chunks_fts (child_id, embedding_text) "
                    "VALUES (:child_id, :tokens)"
                ),
                {"child_id": child_id, "tokens": tokens},
            )

    def delete(self, child_id: int) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text("DELETE FROM child_chunks_fts WHERE child_id = :child_id"),
                {"child_id": child_id},
            )

    def delete_many(self, child_ids: Iterable[int]) -> None:
        for child_id in child_ids:
            self.delete(child_id)

    def search(self, query: str, *, top_k: int) -> list[int]:
        """按 BM25 召回，多词之间使用 OR 语义（召回优先，排序由 BM25 决定）。"""

        tokens = tokenize(query).split()
        if not tokens:
            return []
        match_query = " OR ".join(_quote(token) for token in tokens)
        statement = text(
            "SELECT child_id FROM child_chunks_fts "
            "WHERE child_chunks_fts MATCH :match_query "
            "ORDER BY bm25(child_chunks_fts) LIMIT :top_k"
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                statement, {"match_query": match_query, "top_k": top_k}
            )
            return [int(row[0]) for row in rows]
