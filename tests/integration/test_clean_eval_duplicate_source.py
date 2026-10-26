"""独立评测副本清理守卫的离线集成测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.infra.db import (
    ChildChunk,
    KnowledgeCollectionSource,
    KnowledgeSource,
    ParentChunk,
    SourceAlias,
    create_db_engine,
    create_session_factory,
    init_db,
)
from app.infra.fts import FtsIndex
from app.infra.vector import VectorIndex
from scripts import clean_eval_duplicate_source as cleanup

NOW = "2026-10-26T00:00:00Z"


def _build_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, cleanup.ExpectedCleanup]:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    engine: Engine = create_db_engine(source / cleanup.APP_DB)
    init_db(engine)
    session_factory = create_session_factory(engine)
    fts = FtsIndex(engine)
    fts.ensure_schema()

    keeper_parent_id = 100
    duplicate_parent_id = 101
    keeper_child_id = 200
    duplicate_child_id = 201
    other_source_id = 115
    other_parent_id = 102
    other_child_id = 202
    with session_factory() as session:
        for source_id, url, parent_id, child_id in (
            (
                cleanup.KEEPER_ID,
                cleanup.KEEPER_URL,
                keeper_parent_id,
                keeper_child_id,
            ),
            (
                cleanup.DUPLICATE_ID,
                cleanup.DUPLICATE_URL,
                duplicate_parent_id,
                duplicate_child_id,
            ),
        ):
            session.add(
                KnowledgeSource(
                    id=source_id,
                    url=url,
                    canonical_url=url,
                    title=cleanup.TITLE,
                    content_hash=cleanup.CONTENT_HASH,
                    fingerprint_version=None,
                    status="completed",
                    identity_status="legacy_unverified",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.flush()
            session.add(
                SourceAlias(
                    source_id=source_id,
                    url=url,
                    normalized_url=url,
                    alias_type="legacy_provisional",
                    created_at=NOW,
                )
            )
            session.add(
                KnowledgeCollectionSource(
                    collection_id="default",
                    source_id=source_id,
                    added_at=NOW,
                )
            )
            session.add(
                ParentChunk(
                    id=parent_id,
                    source_id=source_id,
                    ordinal=0,
                    heading_path="Heading",
                    content="Same parent content",
                )
            )
            session.flush()
            session.add(
                ChildChunk(
                    id=child_id,
                    source_id=source_id,
                    parent_id=parent_id,
                    ordinal=0,
                    text="Same child content",
                    embedding_text="same child content",
                    content_type="paragraph",
                    heading_path="Heading",
                )
            )
        session.commit()

    with session_factory() as session:
        session.add(
            KnowledgeSource(
                id=other_source_id,
                url="https://example.com/other",
                canonical_url="https://example.com/other",
                title="Other source",
                content_hash="b" * 64,
                fingerprint_version=None,
                status="completed",
                identity_status="legacy_unverified",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            SourceAlias(
                source_id=other_source_id,
                url="https://example.com/other",
                normalized_url="https://example.com/other",
                alias_type="legacy_provisional",
                created_at=NOW,
            )
        )
        session.add(
            KnowledgeCollectionSource(
                collection_id="default",
                source_id=other_source_id,
                added_at=NOW,
            )
        )
        session.add(
            ParentChunk(
                id=other_parent_id,
                source_id=other_source_id,
                ordinal=0,
                heading_path="Other",
                content="Other parent content",
            )
        )
        session.flush()
        session.add(
            ChildChunk(
                id=other_child_id,
                source_id=other_source_id,
                parent_id=other_parent_id,
                ordinal=0,
                text="Other child content",
                embedding_text="other child content",
                content_type="paragraph",
                heading_path="Other",
            )
        )
        session.commit()

    for child_id in (keeper_child_id, duplicate_child_id, other_child_id):
        fts.upsert(child_id, f"same child content {child_id}")
    vector = VectorIndex(source / "chroma")
    vector.upsert(
        child_ids=[
            keeper_child_id,
            duplicate_child_id,
            other_child_id,
        ],
        embeddings=[
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ],
        metadatas=[
            {"source_id": cleanup.KEEPER_ID},
            {"source_id": cleanup.DUPLICATE_ID},
            {"source_id": other_source_id},
        ],
    )
    vector.close()
    engine.dispose()
    for sidecar_name in cleanup.SQLITE_SIDECARS:
        sidecar = source / sidecar_name
        if sidecar.exists():
            sidecar.unlink()

    expectation = cleanup.ExpectedCleanup(
        keeper_id=cleanup.KEEPER_ID,
        duplicate_id=cleanup.DUPLICATE_ID,
        keeper_url=cleanup.KEEPER_URL,
        duplicate_url=cleanup.DUPLICATE_URL,
        title=cleanup.TITLE,
        content_hash=cleanup.CONTENT_HASH,
        fingerprint_version=None,
        keeper_aliases=frozenset(
            {(cleanup.KEEPER_URL, "legacy_provisional")}
        ),
        duplicate_aliases=frozenset(
            {(cleanup.DUPLICATE_URL, "legacy_provisional")}
        ),
        keeper_parent_ids=frozenset({keeper_parent_id}),
        duplicate_parent_ids=frozenset({duplicate_parent_id}),
        keeper_child_ids=frozenset({keeper_child_id}),
        duplicate_child_ids=frozenset({duplicate_child_id}),
        collection_memberships=frozenset(
            {
                ("default", cleanup.KEEPER_ID),
                ("default", cleanup.DUPLICATE_ID),
            }
        ),
        counts=cleanup.ExpectedCounts(
            sources=3,
            completed_sources=3,
            parents=3,
            children=3,
            aliases=3,
        ),
    )
    return source, target, expectation


def _options(source: Path, target: Path) -> cleanup.CleanupOptions:
    return cleanup.CleanupOptions(
        source_dir=source,
        target_dir=target,
        confirm_cleanup=True,
    )


def test_cli_refuses_without_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"

    exit_code = cleanup.main(
        ["--source-dir", str(source), "--target-dir", str(target)]
    )

    failure = json.loads(capsys.readouterr().err)
    assert exit_code == 1
    assert failure["stage"] == "confirmation"
    assert not target.exists()


def test_rejects_existing_or_nested_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    existing_target = tmp_path / "existing"
    existing_target.mkdir()

    with pytest.raises(cleanup.CleanupError, match="拒绝覆盖") as existing:
        cleanup._resolve_clean_paths(_options(source, existing_target))
    assert existing.value.stage == "paths"

    nested_target = source / "nested"
    with pytest.raises(cleanup.CleanupError, match="内部") as nested:
        cleanup._resolve_clean_paths(_options(source, nested_target))
    assert nested.value.stage == "paths"


def test_preflight_failure_does_not_create_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / cleanup.APP_DB).write_bytes(b"not a sqlite database")
    chroma_path = source / "chroma"
    chroma_path.mkdir()
    (chroma_path / "chroma.sqlite3").write_bytes(b"not a sqlite database")
    target = tmp_path / "target"

    with pytest.raises(cleanup.CleanupError) as failure:
        cleanup.run_cleanup(
            _options(source, target),
            cleanup.DEFAULT_EXPECTATION,
        )

    assert failure.value.stage == "source_preflight"
    assert not target.exists()


def test_cleanup_succeeds_on_isolated_copy(tmp_path: Path) -> None:
    source, target, expectation = _build_fixture(tmp_path)
    source_hashes = cleanup._hash_tree(source, "test")

    report = cleanup.run_cleanup(_options(source, target), expectation)

    assert report["status"] == "success"
    assert report["original_unchanged"] is True
    assert cleanup._hash_tree(source, "test") == source_hashes
    assert (target / cleanup.REPORT_FILE).is_file()
    state = cleanup._read_state(target, "test")
    assert all(
        int(source_row[0]) != cleanup.DUPLICATE_ID
        for source_row in state.source_rows
    )
    assert state.fts_ids == (200, 202)
    assert state.chroma_rows == (
        (200, cleanup.KEEPER_ID),
        (202, 115),
    )
    assert cleanup._aliases_for_source(state, cleanup.KEEPER_ID) == frozenset(
        {
            (cleanup.KEEPER_URL, "legacy_provisional"),
            (cleanup.DUPLICATE_URL, "legacy_provisional"),
        }
    )


def test_post_verification_uses_all_remaining_child_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target, expectation = _build_fixture(tmp_path)
    verified_id_sets: list[frozenset[int]] = []
    real_verify = cleanup._verify_chroma_ids

    def record_verification(
        target_dir: Path,
        expected_all_child_ids: frozenset[int],
    ) -> None:
        verified_id_sets.append(expected_all_child_ids)
        real_verify(target_dir, expected_all_child_ids)

    monkeypatch.setattr(
        cleanup,
        "_verify_chroma_ids",
        record_verification,
    )

    cleanup.run_cleanup(_options(source, target), expectation)

    assert verified_id_sets == [frozenset({200, 202})]
    assert len(verified_id_sets[0]) > len(expectation.keeper_child_ids)


def test_copy_preflight_failure_stops_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target, expectation = _build_fixture(tmp_path)
    validate_calls = 0
    cleanup_calls: list[Path] = []
    real_validate = cleanup._validate_initial_state

    def fail_second_validation(
        directory: Path,
        state: cleanup.EvaluationState,
        expected: cleanup.ExpectedCleanup,
        stage: str,
    ) -> None:
        nonlocal validate_calls
        validate_calls += 1
        if validate_calls == 2:
            raise cleanup.CleanupError("copy_preflight", "simulated failure")
        real_validate(directory, state, expected, stage)

    def record_cleanup(path: Path, _expectation: cleanup.ExpectedCleanup) -> None:
        cleanup_calls.append(path)

    monkeypatch.setattr(
        cleanup,
        "_validate_initial_state",
        fail_second_validation,
    )
    monkeypatch.setattr(cleanup, "_apply_cleanup", record_cleanup)

    with pytest.raises(cleanup.CleanupError) as failure:
        cleanup.run_cleanup(_options(source, target), expectation)

    assert failure.value.stage == "copy_preflight"
    assert validate_calls == 2
    assert cleanup_calls == []
    assert not (target / cleanup.REPORT_FILE).exists()


def test_delete_failure_is_reported_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target, expectation = _build_fixture(tmp_path)
    source_hashes = cleanup._hash_tree(source, "test")
    delete_calls: list[int] = []

    def fail_delete(
        _source_service: object,
        source_id: int,
    ) -> None:
        delete_calls.append(source_id)
        raise RuntimeError("simulated delete failure")

    monkeypatch.setattr(cleanup.SourceService, "delete", fail_delete)

    with pytest.raises(cleanup.CleanupError) as failure:
        cleanup.run_cleanup(_options(source, target), expectation)

    assert failure.value.stage == "source_delete"
    assert delete_calls == [cleanup.DUPLICATE_ID]
    assert cleanup._hash_tree(source, "test") == source_hashes
    assert not (target / cleanup.REPORT_FILE).exists()


def test_post_verification_failure_is_not_marked_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target, expectation = _build_fixture(tmp_path)

    def fail_validation(
        _state: cleanup.EvaluationState,
        _expected: cleanup.EvaluationState,
        _expectation: cleanup.ExpectedCleanup,
    ) -> None:
        raise cleanup.CleanupError(
            "post_verification",
            "simulated verification failure",
        )

    monkeypatch.setattr(
        cleanup,
        "_validate_cleaned_state",
        fail_validation,
    )

    with pytest.raises(cleanup.CleanupError) as failure:
        cleanup.run_cleanup(_options(source, target), expectation)

    assert failure.value.stage == "post_verification"
    assert not (target / cleanup.REPORT_FILE).exists()
