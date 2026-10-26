"""来源正文只读预览服务。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select

from app.core.errors import AppError, ErrorCode
from app.infra.db import (
    ChildChunk,
    KnowledgeSource,
    ParentChunk,
    SourceAlias,
    create_db_engine,
    create_session_factory,
    init_db,
)
from app.services.source_inspection import SourceInspectionService

NOW = "2026-10-26T00:00:00Z"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, object]]:
    engine: Engine = create_db_engine(tmp_path / "app.db")
    init_db(engine)
    session_factory = create_session_factory(engine)
    service = SourceInspectionService(
        session_factory=session_factory,
        parent_limit=2,
    )
    yield {
        "engine": engine,
        "session_factory": session_factory,
        "service": service,
    }
    engine.dispose()


def add_completed_source(env: dict[str, object]) -> int:
    session_factory = env["session_factory"]
    with session_factory() as session:
        source = KnowledgeSource(
            url="https://example.com/submitted",
            canonical_url="https://example.com/final",
            title="RAG 预览",
            content_hash="a" * 64,
            status="completed",
            fingerprint_version="v1",
            language_code="zh",
            identity_status="confirmed",
            identity_verified_at=NOW,
            created_at=NOW,
            updated_at=NOW,
            processed_at=NOW,
            last_checked_at=NOW,
        )
        session.add(source)
        session.flush()
        source_id = source.id

        session.add_all(
            [
                SourceAlias(
                    source_id=source_id,
                    url="https://example.com/submitted",
                    normalized_url="https://example.com/submitted",
                    alias_type="submitted",
                    created_at="2026-10-26T00:00:00Z",
                ),
                SourceAlias(
                    source_id=source_id,
                    url="https://example.com/final",
                    normalized_url="https://example.com/final",
                    alias_type="final",
                    created_at="2026-10-26T00:00:01Z",
                ),
            ]
        )
        parents: dict[int, ParentChunk] = {}
        for ordinal in (2, 0, 1):
            parent = ParentChunk(
                source_id=source_id,
                ordinal=ordinal,
                heading_path=f"章节 {ordinal}",
                content=f"Parent 正文 {ordinal}",
            )
            session.add(parent)
            session.flush()
            parents[ordinal] = parent

        child_ordinals = {0: (1, 0), 1: (0,), 2: (1, 0)}
        for parent_ordinal, ordinals in child_ordinals.items():
            parent = parents[parent_ordinal]
            for child_ordinal in ordinals:
                session.add(
                    ChildChunk(
                        source_id=source_id,
                        parent_id=parent.id,
                        ordinal=child_ordinal,
                        text=f"Child {parent_ordinal}-{child_ordinal}",
                        embedding_text="internal",
                        content_type="paragraph",
                        heading_path=parent.heading_path,
                    )
                )
        session.commit()
        return source_id


def test_returns_metadata_relations_and_ordered_parent_page(
    env: dict[str, object],
) -> None:
    source_id = add_completed_source(env)
    service: SourceInspectionService = env["service"]

    content = service.get_content(source_id, page=1, size=2)

    assert content.source_id == source_id
    assert content.submitted_url == "https://example.com/submitted"
    assert content.final_url == "https://example.com/final"
    assert content.title == "RAG 预览"
    assert content.content_hash == "a" * 64
    assert content.language_code == "zh"
    assert [alias.alias_type for alias in content.aliases] == [
        "submitted",
        "final",
    ]
    assert content.parent_total == 3
    assert content.child_total == 5
    assert [parent.ordinal for parent in content.parents] == [0, 1]
    assert [
        child.ordinal for child in content.parents[0].children
    ] == [0, 1]
    assert [child.ordinal for child in content.parents[1].children] == [0]


def test_paginates_by_parent_and_allows_page_past_end(
    env: dict[str, object],
) -> None:
    source_id = add_completed_source(env)
    service: SourceInspectionService = env["service"]

    second_page = service.get_content(source_id, page=2, size=2)
    past_end = service.get_content(source_id, page=3, size=2)

    assert [parent.ordinal for parent in second_page.parents] == [2]
    assert [child.ordinal for child in second_page.parents[0].children] == [
        0,
        1,
    ]
    assert past_end.parents == ()
    assert past_end.parent_total == 3
    assert past_end.child_total == 5


def test_rejects_invalid_pagination(env: dict[str, object]) -> None:
    source_id = add_completed_source(env)
    service: SourceInspectionService = env["service"]

    for page, size in ((0, 1), (1, 0), (1, 3)):
        with pytest.raises(AppError) as excinfo:
            service.get_content(source_id, page=page, size=size)
        assert excinfo.value.code == ErrorCode.INVALID_REQUEST


def test_rejects_non_positive_parent_limit(
    env: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="parent_limit"):
        SourceInspectionService(
            session_factory=env["session_factory"],
            parent_limit=0,
        )


@pytest.mark.parametrize("status", ["pending", "processing", "failed", "deleting"])
def test_rejects_non_completed_source(
    env: dict[str, object],
    status: str,
) -> None:
    session_factory = env["session_factory"]
    with session_factory() as session:
        source = KnowledgeSource(
            url=f"https://example.com/{status}",
            canonical_url=f"https://example.com/{status}",
            status=status,
            identity_status="pending",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(source)
        session.commit()
        source_id = source.id
    service: SourceInspectionService = env["service"]

    with pytest.raises(AppError) as excinfo:
        service.get_content(source_id, page=1, size=2)

    assert excinfo.value.code == ErrorCode.SOURCE_CONTENT_NOT_READY


def test_rejects_completed_source_without_chunks(
    env: dict[str, object],
) -> None:
    session_factory = env["session_factory"]
    with session_factory() as session:
        source = KnowledgeSource(
            url="https://example.com/empty",
            canonical_url="https://example.com/empty",
            status="completed",
            identity_status="confirmed",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(source)
        session.commit()
        source_id = source.id
    service: SourceInspectionService = env["service"]

    with pytest.raises(AppError) as excinfo:
        service.get_content(source_id, page=1, size=2)

    assert excinfo.value.code == ErrorCode.SOURCE_CONTENT_NOT_READY


def test_read_does_not_modify_authoritative_data(
    env: dict[str, object],
) -> None:
    source_id = add_completed_source(env)
    session_factory = env["session_factory"]
    with session_factory() as session:
        before = (
            session.scalar(select(func.count()).select_from(KnowledgeSource)),
            session.scalar(select(func.count()).select_from(ParentChunk)),
            session.scalar(select(func.count()).select_from(ChildChunk)),
        )
    service: SourceInspectionService = env["service"]
    service.get_content(source_id, page=1, size=2)
    with session_factory() as session:
        after = (
            session.scalar(select(func.count()).select_from(KnowledgeSource)),
            session.scalar(select(func.count()).select_from(ParentChunk)),
            session.scalar(select(func.count()).select_from(ChildChunk)),
        )

    assert after == before
