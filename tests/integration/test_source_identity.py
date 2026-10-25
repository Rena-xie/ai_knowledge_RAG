"""来源身份、别名和正文去重。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.domain.fingerprint import compute_content_fingerprint
from app.domain.source_links import RelatedDocumentLinks
from app.infra.db import (
    KnowledgeSource,
    SourceAlias,
    create_db_engine,
    create_session_factory,
    init_db,
)
from app.services.source_identity import (
    IdentityConfirmation,
    IdentityPlanRequest,
    SourceIdentityService,
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, object]]:
    engine: Engine = create_db_engine(tmp_path / "app.db")
    init_db(engine)
    factory = create_session_factory(engine)
    service = SourceIdentityService(
        session_factory=factory,
        clock=lambda: "2026-10-25T00:00:00Z",
    )
    yield {"engine": engine, "factory": factory, "service": service}
    engine.dispose()


def add_source(
    env: dict[str, object],
    *,
    url: str,
    status: str = "pending",
) -> int:
    factory = env["factory"]
    with factory() as session:
        source = KnowledgeSource(
            url=url,
            canonical_url=url,
            status=status,
            identity_status="pending",
            created_at="2026-10-25T00:00:00Z",
            updated_at="2026-10-25T00:00:00Z",
        )
        session.add(source)
        session.commit()
        return source.id


def test_confirms_final_url_and_aliases(env: dict[str, object]) -> None:
    factory = env["factory"]
    service: SourceIdentityService = env["service"]
    source_id = add_source(env, url="https://example.com/start")
    related = RelatedDocumentLinks(
        declared_canonical="https://example.com/article",
        amp_html="https://example.com/article/amp",
    )

    resolution = service.plan_final_url(
        IdentityPlanRequest(
            source_id=source_id,
            final_url="https://example.com/article",
            redirect_urls=("https://example.com/start",),
            related_links=related,
        )
    )
    assert resolution.duplicate_of_source_id is None

    with factory() as session:
        service.confirm_final_url(
            session,
            IdentityConfirmation(
                source_id=source_id,
                submitted_url="https://example.com/start?utm_source=feed",
                final_url=resolution.canonical_url,
                redirect_urls=("https://example.com/start",),
                related_links=related,
            ),
        )
        session.commit()

    with factory() as session:
        source = session.get(KnowledgeSource, source_id)
        assert source is not None
        assert source.canonical_url == "https://example.com/article"
        assert source.identity_status == "confirmed"
        aliases = list(
            session.query(SourceAlias).filter_by(source_id=source_id).all()
        )
        assert {
            alias.normalized_url: alias.alias_type for alias in aliases
        } == {
            "https://example.com/start": "submitted",
            "https://example.com/article": "final",
            "https://example.com/article/amp": "amp",
        }


def test_plan_detects_existing_final_url(env: dict[str, object]) -> None:
    service: SourceIdentityService = env["service"]
    existing_id = add_source(
        env,
        url="https://example.com/article",
        status="completed",
    )
    candidate_id = add_source(env, url="https://example.com/start")

    resolution = service.plan_final_url(
        IdentityPlanRequest(
            source_id=candidate_id,
            final_url="https://example.com/article",
        )
    )

    assert resolution.duplicate_of_source_id == existing_id
    assert resolution.duplicate_reason == "duplicate_url"


def test_refresh_can_replace_final_url_without_changing_source_id(
    env: dict[str, object],
) -> None:
    factory = env["factory"]
    service: SourceIdentityService = env["service"]
    source_id = add_source(env, url="https://example.com/old")
    with factory() as session:
        service.confirm_final_url(
            session,
            IdentityConfirmation(
                source_id=source_id,
                submitted_url="https://example.com/old",
                final_url="https://example.com/old",
            ),
        )
        session.commit()

    resolution = service.plan_final_url(
        IdentityPlanRequest(
            source_id=source_id,
            final_url="https://example.com/new",
        )
    )
    with factory() as session:
        service.confirm_final_url(
            session,
            IdentityConfirmation(
                source_id=source_id,
                submitted_url="https://example.com/old",
                final_url=resolution.canonical_url,
            ),
        )
        session.commit()

    with factory() as session:
        source = session.get(KnowledgeSource, source_id)
        assert source is not None
        assert source.id == source_id
        assert source.canonical_url == "https://example.com/new"
        assert (
            session.query(SourceAlias)
            .filter_by(
                source_id=source_id,
                normalized_url="https://example.com/old",
            )
            .one()
            .alias_type
            == "submitted"
        )


def test_finds_earliest_matching_content(env: dict[str, object]) -> None:
    factory = env["factory"]
    service: SourceIdentityService = env["service"]
    fingerprint = compute_content_fingerprint("# 标题\n正文")
    first_id = add_source(env, url="https://example.com/a", status="completed")
    second_id = add_source(env, url="https://example.com/b", status="completed")
    with factory() as session:
        for source_id in (first_id, second_id):
            source = session.get(KnowledgeSource, source_id)
            source.content_hash = fingerprint.value
            source.fingerprint_version = fingerprint.version
        session.commit()

    duplicate = service.find_content_duplicate(
        fingerprint, exclude_source_id=999
    )

    assert duplicate == first_id


def test_merges_aliases_before_duplicate_source_cleanup(
    env: dict[str, object],
) -> None:
    factory = env["factory"]
    service: SourceIdentityService = env["service"]
    target_id = add_source(env, url="https://example.com/target")
    candidate_id = add_source(env, url="https://example.com/candidate")
    with factory() as session:
        session.add_all(
            [
                SourceAlias(
                    source_id=target_id,
                    url="https://example.com/target",
                    normalized_url="https://example.com/target",
                    alias_type="submitted",
                    created_at="2026-10-25T00:00:00Z",
                ),
                SourceAlias(
                    source_id=candidate_id,
                    url="https://example.com/new",
                    normalized_url="https://example.com/new",
                    alias_type="submitted",
                    created_at="2026-10-25T00:00:00Z",
                ),
            ]
        )
        session.commit()

    service.merge_aliases(from_source_id=candidate_id, to_source_id=target_id)

    with factory() as session:
        assert (
            session.query(SourceAlias)
            .filter_by(
                source_id=target_id,
                normalized_url="https://example.com/new",
            )
            .count()
            == 1
        )
