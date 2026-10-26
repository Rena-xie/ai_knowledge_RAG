"""派生索引维护：重建、一致性校验与备份（见《项目开发说明_v1.md》5.4、5.6）。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import utc_now_iso
from app.core.context import bind_source_id
from app.core.settings import Settings
from app.infra.db import ChildChunk, IndexMeta
from app.infra.embedding import EmbeddingClient
from app.infra.fts import FtsIndex
from app.infra.vector import VectorIndex

DEFAULT_CHUNKER_VERSION = "v1"

ChildRow = tuple[int, int, str]


@dataclass(frozen=True)
class ReindexReport:
    """重建结果统计。"""

    sources: int
    children: int
    fts_entries: int
    vector_entries: int
    pruned_vectors: int


@dataclass(frozen=True)
class ConsistencyReport:
    """三处数据一致性报告。"""

    db_children: int
    fts_entries: int
    vector_entries: int
    missing_in_fts: tuple[int, ...]
    missing_in_vector: tuple[int, ...]
    extra_in_fts: tuple[int, ...]
    extra_in_vector: tuple[int, ...]

    @property
    def consistent(self) -> bool:
        return not (
            self.missing_in_fts
            or self.missing_in_vector
            or self.extra_in_fts
            or self.extra_in_vector
        )


@dataclass(frozen=True)
class BackupReport:
    """备份产物路径。"""

    target: Path
    sqlite_backup: Path
    chroma_backup: Path
    manifest: Path


class MaintenanceService:
    """从 SQLite 权威数据重建派生索引，并维护索引元数据。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        fts_index: FtsIndex,
        vector_index: VectorIndex,
        embedder: EmbeddingClient,
        settings: Settings,
        chunker_version: str = DEFAULT_CHUNKER_VERSION,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self._session_factory = session_factory
        self._fts_index = fts_index
        self._vector_index = vector_index
        self._embedder = embedder
        self._settings = settings
        self._chunker_version = chunker_version
        self._clock = clock

    def reindex_all(self) -> ReindexReport:
        """全量重建：清空全文索引、清除孤儿向量并按权威数据重写。"""

        rows = self._children()
        self._fts_index.reset()
        for child_id, _source_id, text in rows:
            self._fts_index.upsert(child_id, text)
        authoritative = {child_id for child_id, _source_id, _text in rows}
        stale = sorted(set(self._vector_index.all_ids()) - authoritative)
        self._vector_index.delete_many(stale)
        if rows:
            vectors = self._embedder.embed([text for _child, _source, text in rows])
            self._vector_index.upsert(
                [child_id for child_id, _source, _text in rows],
                vectors,
                [{"source_id": source_id} for _child, source_id, _text in rows],
            )
        self._write_meta()
        return ReindexReport(
            sources=len({source_id for _child, source_id, _text in rows}),
            children=len(rows),
            fts_entries=len(self._fts_index.all_ids()),
            vector_entries=self._vector_index.count(),
            pruned_vectors=len(stale),
        )

    def reindex_source(self, source_id: int) -> ReindexReport:
        """按来源重建其 FTS5 与 Chroma 数据。"""

        rows = self._children(source_id=source_id)
        self._vector_index.delete_by_source(source_id)
        self._fts_index.delete_many([child_id for child_id, _s, _t in rows])
        if rows:
            with bind_source_id(source_id):
                vectors = self._embedder.embed(
                    [text for _child, _source, text in rows]
                )
            for child_id, _source, text in rows:
                self._fts_index.upsert(child_id, text)
            self._vector_index.upsert(
                [child_id for child_id, _source, _text in rows],
                vectors,
                [{"source_id": source_id} for _ in rows],
            )
        self._write_meta()
        return ReindexReport(
            sources=1 if rows else 0,
            children=len(rows),
            fts_entries=len(rows),
            vector_entries=self._vector_index.count(),
            pruned_vectors=0,
        )

    def _children(self, source_id: int | None = None) -> list[ChildRow]:
        with self._session_factory() as session:
            statement = select(
                ChildChunk.id, ChildChunk.source_id, ChildChunk.embedding_text
            ).order_by(ChildChunk.id)
            if source_id is not None:
                statement = statement.where(ChildChunk.source_id == source_id)
            return [
                (row[0], row[1], row[2]) for row in session.execute(statement).all()
            ]

    def _write_meta(self) -> None:
        values = {
            "embedding_model": self._settings.embedding_model,
            "chunker_version": self._chunker_version,
            "rebuilt_at": self._clock(),
        }
        with self._session_factory() as session:
            for key, value in values.items():
                row = session.get(IndexMeta, key)
                if row is None:
                    session.add(IndexMeta(key=key, value=value))
                else:
                    row.value = value
            session.commit()

    def check_consistency(self) -> ConsistencyReport:
        """比对 SQLite（权威）、FTS5 与 Chroma 的 child_id 集合。"""

        db_ids = {child_id for child_id, _source, _text in self._children()}
        fts_ids = set(self._fts_index.all_ids())
        vector_ids = set(self._vector_index.all_ids())
        return ConsistencyReport(
            db_children=len(db_ids),
            fts_entries=len(fts_ids),
            vector_entries=len(vector_ids),
            missing_in_fts=tuple(sorted(db_ids - fts_ids)),
            missing_in_vector=tuple(sorted(db_ids - vector_ids)),
            extra_in_fts=tuple(sorted(fts_ids - db_ids)),
            extra_in_vector=tuple(sorted(vector_ids - db_ids)),
        )

    def backup(self, target: Path) -> BackupReport:
        """备份 SQLite（在线一致性快照）与 Chroma 目录；建议停服执行。"""

        target.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        sqlite_backup = target / f"app-{timestamp}.db"
        self._backup_sqlite(self._settings.sqlite_path, sqlite_backup)
        chroma_backup = target / f"chroma-{timestamp}"
        if self._settings.chroma_path.exists():
            shutil.copytree(self._settings.chroma_path, chroma_backup)
        manifest = target / f"manifest-{timestamp}.json"
        manifest.write_text(
            json.dumps(
                {
                    "created_at": self._clock(),
                    "sqlite_source": str(self._settings.sqlite_path),
                    "chroma_source": str(self._settings.chroma_path),
                    "sqlite_backup": sqlite_backup.name,
                    "chroma_backup": chroma_backup.name,
                    "note": "备份应在服务停止状态下执行；恢复后请运行一致性校验",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return BackupReport(
            target=target,
            sqlite_backup=sqlite_backup,
            chroma_backup=chroma_backup,
            manifest=manifest,
        )

    @staticmethod
    def _backup_sqlite(source: Path, destination: Path) -> None:
        source_connection = sqlite3.connect(source)
        try:
            destination_connection = sqlite3.connect(destination)
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
        finally:
            source_connection.close()
