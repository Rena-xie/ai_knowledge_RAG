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
