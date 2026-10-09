"""派生索引维护：重建、一致性校验与备份（见《项目介绍.md》5.4、5.6）。"""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from app.cli.maintenance import run_backup, run_consistency
from app.core.settings import Settings
from app.infra.db import (
    ChildChunk,
    IndexMeta,
    KnowledgeSource,
    ParentChunk,
    create_db_engine,
    create_session_factory,
    init_db,
)
from app.infra.fts import FtsIndex
from app.infra.vector import VectorIndex
from app.services.maintenance import MaintenanceService

NOW = "2026-10-09T00:00:00Z"


class FakeEmbedder:
    """按文本长度生成确定性向量，不访问外部 API。"""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(text) + 1), 1.0, 0.0] for text in texts]


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        embedding_api_base="https://example.invalid/v1",
        embedding_api_key="test",
        llm_api_base="https://example.invalid/v1",
        llm_api_key="test",
        sqlite_path=tmp_path / "app.db",
        chroma_path=tmp_path / "chroma",
    )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, object]]:
    settings = build_settings(tmp_path)
    engine: Engine = create_db_engine(settings.sqlite_path)
    init_db(engine)
    fts = FtsIndex(engine)
    fts.ensure_schema()
    vector = VectorIndex(settings.chroma_path)
    embedder = FakeEmbedder()
    service = MaintenanceService(
        session_factory=create_session_factory(engine),
        fts_index=fts,
        vector_index=vector,
        embedder=embedder,
        settings=settings,
    )
    yield {
        "engine": engine,
        "session_factory": create_session_factory(engine),
        "fts": fts,
        "vector": vector,
        "embedder": embedder,
        "service": service,
        "settings": settings,
        "tmp_path": tmp_path,
    }
    engine.dispose()


def seed_source(
    env: dict[str, object],
    *,
    url: str,
    texts: Sequence[str] = ("BM25 稀疏检索", "向量召回"),
) -> int:
    with env["session_factory"]() as session:
        source = KnowledgeSource(
            url=url,
            canonical_url=url,
            status="completed",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(source)
        session.flush()
        parent = ParentChunk(
            source_id=source.id, ordinal=0, heading_path="H", content="父级内容"
        )
        session.add(parent)
        session.flush()
        for index, text in enumerate(texts):
            session.add(
                ChildChunk(
                    source_id=source.id,
                    parent_id=parent.id,
                    ordinal=index,
                    text=text,
                    embedding_text=text,
                    content_type="paragraph",
                    heading_path="H",
                )
            )
        session.commit()
        return source.id


def test_reindex_all_rebuilds_both_indexes(env: dict[str, object]) -> None:
    seed_source(env, url="https://example.com/a")
    seed_source(env, url="https://example.com/b")
    service: MaintenanceService = env["service"]

    report = service.reindex_all()

    assert report.sources == 2
    assert report.children == 4
    assert report.fts_entries == 4
    assert report.vector_entries == 4
    assert env["fts"].search("BM25", top_k=5)
    assert env["vector"].count() == 4


def test_reindex_all_prunes_stale_entries(env: dict[str, object]) -> None:
    seed_source(env, url="https://example.com/a")
    service: MaintenanceService = env["service"]
    service.reindex_all()
    env["vector"].upsert([9999], [[1.0, 2.0, 0.0]], [{"source_id": 99}])
    env["fts"].upsert(8888, "孤儿条目")
    assert env["vector"].count() == 3

    report = service.reindex_all()

    assert report.pruned_vectors == 1
    assert env["vector"].count() == 2
    assert 8888 not in env["fts"].all_ids()


def test_reindex_source_rebuilds_only_that_source(env: dict[str, object]) -> None:
    first = seed_source(env, url="https://example.com/a")
    second = seed_source(env, url="https://example.com/b")
    service: MaintenanceService = env["service"]
    service.reindex_all()
    env["vector"].delete_by_source(second)
    assert env["vector"].count() == 2

    report = service.reindex_source(second)

    assert report.children == 2
    assert env["vector"].count() == 4
    assert first != second


def test_reindex_updates_index_meta(env: dict[str, object]) -> None:
    seed_source(env, url="https://example.com/a")
    service: MaintenanceService = env["service"]

    service.reindex_all()

    with env["session_factory"]() as session:
        meta = {
            row.key: row.value
            for row in session.scalars(select(IndexMeta)).all()
        }
    assert meta["embedding_model"] == "BAAI/bge-m3"
    assert meta["chunker_version"] == "v1"
    assert meta["rebuilt_at"]


def test_consistency_report_is_clean_after_reindex(env: dict[str, object]) -> None:
    seed_source(env, url="https://example.com/a")
    service: MaintenanceService = env["service"]
    service.reindex_all()

    report = service.check_consistency()

    assert report.consistent is True
    assert report.db_children == 2
    assert report.fts_entries == 2
    assert report.vector_entries == 2


def test_consistency_report_detects_drift(env: dict[str, object]) -> None:
    seed_source(env, url="https://example.com/a")
    service: MaintenanceService = env["service"]
    service.reindex_all()
    env["vector"].delete_many([1])
    env["fts"].upsert(7777, "孤儿条目")

    report = service.check_consistency()

    assert report.consistent is False
    assert report.missing_in_vector == (1,)
    assert report.extra_in_fts == (7777,)


def test_backup_creates_restorable_copy(env: dict[str, object]) -> None:
    seed_source(env, url="https://example.com/a")
    service: MaintenanceService = env["service"]
    service.reindex_all()
    target = env["tmp_path"] / "backup"

    report = service.backup(target)

    assert report.sqlite_backup.is_file()
    assert report.chroma_backup.is_dir()
    assert report.manifest.is_file()
    manifest = json.loads(report.manifest.read_text(encoding="utf-8"))
    assert manifest["sqlite_backup"] == report.sqlite_backup.name

    connection = sqlite3.connect(report.sqlite_backup)
    try:
        count = connection.execute("SELECT COUNT(*) FROM child_chunks").fetchone()[0]
    finally:
        connection.close()
    assert count == 2


def test_consistency_command_exit_codes(env: dict[str, object]) -> None:
    seed_source(env, url="https://example.com/a")
    service: MaintenanceService = env["service"]
    service.reindex_all()

    assert run_consistency([], service=service) == 0

    env["vector"].delete_many([1])

    assert run_consistency([], service=service) == 1


def test_backup_command_returns_success(env: dict[str, object]) -> None:
    seed_source(env, url="https://example.com/a")
    service: MaintenanceService = env["service"]
    target = env["tmp_path"] / "cli-backup"

    exit_code = run_backup(["--target", str(target)], service=service)

    assert exit_code == 0
    assert list(target.glob("manifest-*.json"))
