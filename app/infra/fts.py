"""FTS5 全文索引（见《项目开发说明_v1.md》5.1、7.1）。"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import jieba
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

jieba.setLogLevel(logging.WARNING)

CREATE_FTS_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS child_chunks_fts "
    "USING fts5(child_id UNINDEXED, embedding_text)"
)

# 代码符号在 jieba 与 FTS5 的 unicode61 分词器下都会丢失，因此映射为字母 token。
# 不含 `*` 与 `**`：Markdown 的列表符号与加粗标记会造成大量误召回。
SYMBOL_CHARS = frozenset("+-*/%=<>!&|^~")
SYMBOL_TOKENS = {
    "/": "sym_slash",
    "//": "sym_dslash",
    "%": "sym_percent",
    "==": "sym_eq",
    "!=": "sym_ne",
    ">=": "sym_ge",
    "<=": "sym_le",
    "->": "sym_arrow",
    "=>": "sym_fat_arrow",
    "+=": "sym_plus_eq",
    "-=": "sym_minus_eq",
    "*=": "sym_mul_eq",
    "/=": "sym_div_eq",
    "&&": "sym_and",
    "||": "sym_or",
}


def _symbol_tokens(value: str) -> list[str]:
    """抽取代码符号并映射为可被 FTS5 索引的 token。

    仅在符号串至少一侧不是字母数字时才判定为代码符号，避免把 URL 里的
    `://` 或路径分隔符当成运算符；紧跟冒号的符号串（URL 协议）直接忽略
    （见《项目开发说明_v1.md》7.1）。
    """

    tokens: list[str] = []
    length = len(value)
    index = 0
    while index < length:
        if value[index] not in SYMBOL_CHARS:
            index += 1
            continue
        end = index
        while end < length and value[end] in SYMBOL_CHARS:
            end += 1
        after_scheme = index > 0 and value[index - 1] == ":"
        left_bound = index == 0 or not value[index - 1].isalnum()
        right_bound = end == length or not value[end].isalnum()
        if not after_scheme and (left_bound or right_bound):
            mapped = SYMBOL_TOKENS.get(value[index:end])
            if mapped and mapped not in tokens:
                tokens.append(mapped)
        index = end
    return tokens


def tokenize(value: str) -> str:
    """jieba 搜索引擎模式分词 + 代码符号归一化，返回空格分隔的词串。"""

    tokens = [
        token.strip() for token in jieba.cut_for_search(value) if token.strip()
    ]
    tokens.extend(_symbol_tokens(value))
    return " ".join(tokens)


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

    def upsert(
        self,
        child_id: int,
        embedding_text: str,
        *,
        connection: Connection | None = None,
    ) -> None:
        tokens = tokenize(embedding_text)
        if connection is not None:
            self._upsert_on(connection, child_id, tokens)
            return
        with self._engine.begin() as connection:
            self._upsert_on(connection, child_id, tokens)

    @staticmethod
    def _upsert_on(connection: Connection, child_id: int, tokens: str) -> None:
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

    def delete(
        self, child_id: int, *, connection: Connection | None = None
    ) -> None:
        if connection is not None:
            self._delete_on(connection, child_id)
            return
        with self._engine.begin() as connection:
            self._delete_on(connection, child_id)

    @staticmethod
    def _delete_on(connection: Connection, child_id: int) -> None:
        connection.execute(
            text("DELETE FROM child_chunks_fts WHERE child_id = :child_id"),
            {"child_id": child_id},
        )

    def delete_many(
        self,
        child_ids: Iterable[int],
        *,
        connection: Connection | None = None,
    ) -> None:
        for child_id in child_ids:
            self.delete(child_id, connection=connection)

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
