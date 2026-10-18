"""SQLite 权威存储（见《项目开发说明_v1.md》5.1、5.3）。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.infra.db import (
    SCHEMA_VERSION,
    ChildChunk,
    IndexMeta,
    KnowledgeSource,
    ParentChunk,
    create_db_engine,
    create_session_factory,
    init_db,
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_engine = create_db_engine(tmp_path / "app.db")
    init_db(db_engine)
    yield db_engine
    db_engine.dispose()


def make_source(canonical_url: str = "https://example.com/a") -> KnowledgeSource:
    return KnowledgeSource(
        url=canonical_url,
        canonical_url=canonical_url,
        status="pending",
        created_at="2026-10-09T00:00:00Z",
        updated_at="2026-10-09T00:00:00Z",
    )


def test_creates_all_tables(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())

    assert {
        "knowledge_sources",
        "parent_chunks",
        "child_chunks",
        "index_meta",
        "ingestion_jobs",
        "ingestion_job_items",
    } <= tables


def test_enables_wal_busy_timeout_and_foreign_keys(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 5000
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_canonical_url_is_unique(engine: Engine) -> None:
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        session.add(make_source())
        session.commit()

    with session_factory() as session:
        session.add(make_source())
        with pytest.raises(IntegrityError):
            session.commit()


def test_child_ordinal_is_unique_within_parent(engine: Engine) -> None:
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        source = make_source()
        session.add(source)
        session.flush()
        parent = ParentChunk(
            source_id=source.id, ordinal=0, heading_path="A", content="内容"
        )
        session.add(parent)
        session.flush()
        session.add(
            ChildChunk(
                source_id=source.id,
                parent_id=parent.id,
                ordinal=0,
                text="甲",
                embedding_text="A\n甲",
                content_type="paragraph",
                heading_path="A",
            )
        )
        session.commit()

    with session_factory() as session:
        session.add(
            ChildChunk(
                source_id=1,
                parent_id=1,
                ordinal=0,
                text="乙",
                embedding_text="A\n乙",
                content_type="paragraph",
                heading_path="A",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_round_trip_source_with_chunks(engine: Engine) -> None:
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        source = make_source()
        session.add(source)
        session.flush()
        parent = ParentChunk(
            source_id=source.id, ordinal=0, heading_path="A", content="内容"
        )
        session.add(parent)
        session.flush()
        session.add(
            ChildChunk(
                source_id=source.id,
                parent_id=parent.id,
                ordinal=0,
                text="甲",
                embedding_text="A\n甲",
                content_type="paragraph",
                heading_path="A",
            )
        )
        session.commit()
        source_id = source.id

    with session_factory() as session:
        loaded = session.get(KnowledgeSource, source_id)
        assert loaded is not None
        assert loaded.status == "pending"
        children = session.query(ChildChunk).filter_by(source_id=source_id).all()
        assert [child.text for child in children] == ["甲"]


def test_index_meta_round_trip(engine: Engine) -> None:
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        session.add(IndexMeta(key="chunker_version", value="v1"))
        session.commit()

    with session_factory() as session:
        assert session.get(IndexMeta, "chunker_version").value == "v1"


def test_init_db_writes_schema_version(engine: Engine) -> None:
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        assert session.get(IndexMeta, "schema_version").value == SCHEMA_VERSION


def test_init_db_keeps_existing_schema_version(engine: Engine) -> None:
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        session.get(IndexMeta, "schema_version").value = "0"
        session.commit()

    init_db(engine)

    with session_factory() as session:
        assert session.get(IndexMeta, "schema_version").value == "0"


def test_migrates_v1_database(tmp_path: Path) -> None:
    """v1 数据文件升级后保留数据并补上 v2 字段与表。"""

    engine = create_db_engine(tmp_path / "v1.db")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE knowledge_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                canonical_url TEXT NOT NULL UNIQUE,
                title TEXT,
                content_hash TEXT,
                status TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                chunker_version TEXT,
                embedding_model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                processed_at TEXT
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO knowledge_sources
                (url, canonical_url, title, status, created_at, updated_at)
            VALUES
                ('https://example.com/a', 'https://example.com/a',
                 '旧来源', 'completed', '2026-10-01T00:00:00Z',
                 '2026-10-01T00:00:00Z')
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO index_meta (key, value) VALUES ('schema_version', '1')"
        )

    init_db(engine)

    columns = {
        column["name"] for column in inspect(engine).get_columns("knowledge_sources")
    }
    assert "last_checked_at" in columns
    tables = set(inspect(engine).get_table_names())
    assert {"ingestion_jobs", "ingestion_job_items"} <= tables

    session_factory = create_session_factory(engine)
    with session_factory() as session:
        assert session.get(IndexMeta, "schema_version").value == "2"
        source = session.get(KnowledgeSource, 1)
        assert source is not None
        assert source.title == "旧来源"
        assert source.last_checked_at is None
    engine.dispose()
