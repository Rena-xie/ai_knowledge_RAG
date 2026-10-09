"""Chroma 向量索引（见《项目介绍.md》7.1、ADR-007）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import chromadb
from chromadb.config import Settings as ChromaSettings

COLLECTION_NAME = "child_chunks"
COSINE_SPACE = "cosine"


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
        metadatas: Sequence[Mapping[str, object]],
    ) -> None:
        self._collection.upsert(
            ids=[str(child_id) for child_id in child_ids],
            embeddings=[list(vector) for vector in embeddings],
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

    def query(self, embedding: Sequence[float], *, top_k: int) -> list[int]:
        if self._collection.count() == 0:
            return []
        result = self._collection.query(
            query_embeddings=[list(embedding)], n_results=top_k
        )
        ids = result.get("ids") or [[]]
        return [int(value) for value in ids[0]]
