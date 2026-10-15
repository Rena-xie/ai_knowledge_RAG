"""Chroma 向量索引（见《项目介绍.md》7.1、ADR-007）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import chromadb
from chromadb.api.types import Metadata
from chromadb.config import Settings as ChromaSettings

COLLECTION_NAME = "child_chunks"
COSINE_SPACE = "cosine"


@dataclass(frozen=True)
class VectorHit:
    """向量召回命中：`child_id` 与余弦相似度（`1 - cosine 距离`）。"""

    child_id: int
    similarity: float


class VectorIndex:
    """Chroma 持久化集合，显式使用余弦距离。"""

    def __init__(
        self, persist_path: Path, *, collection_name: str = COLLECTION_NAME
    ) -> None:
        persist_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(persist_path),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": COSINE_SPACE},
        )

    @property
    def space(self) -> str:
        metadata = self._collection.metadata or {}
        return str(metadata.get("hnsw:space", ""))

    def count(self) -> int:
        return self._collection.count()

    def upsert(
        self,
        child_ids: Sequence[int],
        embeddings: Sequence[Sequence[float]],
        metadatas: Sequence[Metadata],
    ) -> None:
        vectors: list[Sequence[float]] = [list(vector) for vector in embeddings]
        self._collection.upsert(
            ids=[str(child_id) for child_id in child_ids],
            embeddings=vectors,
            metadatas=[dict(metadata) for metadata in metadatas],
        )

    def delete_by_source(self, source_id: int) -> None:
        self._collection.delete(where={"source_id": source_id})

    def delete_many(self, child_ids: Sequence[int]) -> None:
        if not child_ids:
            return
        self._collection.delete(ids=[str(child_id) for child_id in child_ids])

    def all_ids(self) -> list[int]:
        result = self._collection.get(include=[])
        return [int(value) for value in result.get("ids", [])]

    def query(self, embedding: Sequence[float], *, top_k: int) -> list[VectorHit]:
        if self._collection.count() == 0:
            return []
        query_embedding: list[Sequence[float]] = [list(embedding)]
        result = self._collection.query(
            query_embeddings=query_embedding, n_results=top_k
        )
        ids = result.get("ids") or [[]]
        distances = result.get("distances") or [[]]
        return [
            VectorHit(child_id=int(child_id), similarity=1.0 - float(distance))
            for child_id, distance in zip(ids[0], distances[0], strict=False)
        ]

    def close(self) -> None:
        """释放底层客户端，确保 Windows 下临时数据目录可被删除。"""

        # chromadb 的类型标注把 PersistentClient 声明为 rust 版 ClientAPI，
        # 运行时实际返回带 close() 的 Client（见 chromadb/api/client.py）。
        self._client.close()  # type: ignore[attr-defined]
