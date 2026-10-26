"""在独立冻结评测副本中清理 source 119，并保留 source 114。

脚本只读取原库。副本、清理和报告写入都限制在不存在的新目标目录内。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from app.infra.db import create_db_engine, create_session_factory
from app.infra.fts import FtsIndex
from app.infra.vector import VectorIndex
from app.services.source import SourceService
from app.services.source_identity import SourceIdentityService

APP_DB = "app.db"
CHROMA_DIR = "chroma"
CHROMA_DB = Path(CHROMA_DIR) / "chroma.sqlite3"
REPORT_FILE = "cleanup-report.json"
KEEPER_ID = 114
DUPLICATE_ID = 119
KEEPER_URL = "https://docs.langchain.com/oss/python/deepagents/overview"
DUPLICATE_URL = "https://docs.langchain.com/oss/python/deepagents"
CONTENT_HASH = (
    "567635691fcbe28a894dfb6aefc95962c4021c4e16e99a4f59cc8e0cd089d1a7"
)
TITLE = "Deep Agents overview - Docs by LangChain"
SQLITE_SIDECARS = (
    Path("app.db-wal"),
    Path("app.db-shm"),
    Path("chroma/chroma.sqlite3-wal"),
    Path("chroma/chroma.sqlite3-shm"),
)

Cell = int | str | None
Row = tuple[Cell, ...]


@dataclass(frozen=True)
class CleanupOptions:
    """清理入口参数。"""

    source_dir: Path
    target_dir: Path
    confirm_cleanup: bool


@dataclass(frozen=True)
class ExpectedCounts:
    """执行前的全局行数。"""

    sources: int
    completed_sources: int
    parents: int
    children: int
    aliases: int


@dataclass(frozen=True)
class ExpectedCleanup:
    """唯一允许执行的清理规格。"""

    keeper_id: int
    duplicate_id: int
    keeper_url: str
    duplicate_url: str
    title: str
    content_hash: str
    fingerprint_version: str | None
    keeper_aliases: frozenset[tuple[str, str]]
    duplicate_aliases: frozenset[tuple[str, str]]
    keeper_parent_ids: frozenset[int]
    duplicate_parent_ids: frozenset[int]
    keeper_child_ids: frozenset[int]
    duplicate_child_ids: frozenset[int]
    collection_memberships: frozenset[tuple[str, int]]
    counts: ExpectedCounts


DEFAULT_EXPECTATION = ExpectedCleanup(
    keeper_id=KEEPER_ID,
    duplicate_id=DUPLICATE_ID,
    keeper_url=KEEPER_URL,
    duplicate_url=DUPLICATE_URL,
    title=TITLE,
    content_hash=CONTENT_HASH,
    fingerprint_version=None,
    keeper_aliases=frozenset({(KEEPER_URL, "legacy_provisional")}),
    duplicate_aliases=frozenset({(DUPLICATE_URL, "legacy_provisional")}),
    keeper_parent_ids=frozenset(range(158, 188)),
    duplicate_parent_ids=frozenset(range(247, 277)),
    keeper_child_ids=frozenset(range(2088, 2135)),
    duplicate_child_ids=frozenset(range(2286, 2333)),
    collection_memberships=frozenset(
        {("default", KEEPER_ID), ("default", DUPLICATE_ID)}
    ),
    counts=ExpectedCounts(
        sources=166,
        completed_sources=165,
        parents=1036,
        children=4181,
        aliases=166,
    ),
)


@dataclass(frozen=True)
class EvaluationState:
    """足以比对清理前后数据集合的评测库快照。"""

    source_rows: frozenset[Row]
    parent_rows: frozenset[tuple[int, int]]
    child_rows: frozenset[tuple[int, int]]
    alias_rows: frozenset[Row]
    collection_rows: frozenset[Row]
    target_tags: frozenset[tuple[int, int]]
    target_job_refs: frozenset[tuple[int, int | None, int | None]]
    fts_ids: tuple[int, ...]
    chroma_rows: tuple[tuple[int, int | None], ...]
    chroma_collections: frozenset[tuple[str, str]]


class CleanupError(RuntimeError):
    """带执行阶段的清理失败。"""

    def __init__(self, stage: str, reason: str) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


def _require(condition: bool, stage: str, reason: str) -> None:
    if not condition:
        raise CleanupError(stage, reason)


def _is_link_like(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _resolve_clean_paths(options: CleanupOptions) -> tuple[Path, Path]:
    raw_source = options.source_dir.expanduser()
    raw_target = options.target_dir.expanduser()
    _require(
        not _is_link_like(raw_source),
        "paths",
        "源目录不得是符号链接或目录联接",
    )
    _require(
        raw_source.is_dir(),
        "paths",
        f"源目录不存在或不是目录: {raw_source}",
    )
    source = raw_source.resolve(strict=True)
    target_parent = raw_target.parent.expanduser().resolve(strict=False)
    target = target_parent / raw_target.name
    _require(
        target_parent.is_dir(),
        "paths",
        f"目标父目录不存在或不是目录: {target_parent}",
    )
    _require(
        source != Path(source.anchor),
        "paths",
        "拒绝把文件系统根目录作为评测源目录",
    )
    _require(
        target != Path(target.anchor),
        "paths",
        "非法目标目录",
    )
    _require(
        not target.exists() and not target.is_symlink(),
        "paths",
        f"目标目录已存在，拒绝覆盖: {target}",
    )
    _require(source != target, "paths", "源目录与目标目录不得相同")
    _require(
        not target.is_relative_to(source),
        "paths",
        "目标目录不得位于源目录内部",
    )
    _require(
        not source.is_relative_to(target),
        "paths",
        "源目录不得位于目标目录内部",
    )
    for copied_path in source.rglob("*"):
        _require(
            not _is_link_like(copied_path),
            "paths",
            f"源目录包含符号链接或目录联接: {copied_path}",
        )
    return source, target


def _hash_tree(root: Path, stage: str) -> dict[str, str]:
    try:
        return {
            file_path.relative_to(root).as_posix(): _file_sha256(file_path)
            for file_path in sorted(root.rglob("*"))
            if file_path.is_file()
        }
    except OSError as error:
        raise CleanupError(stage, f"计算文件哈希失败: {error}") from error


def _file_sha256(file_path: Path) -> str:
    with file_path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _assert_source_quiescent(source: Path) -> None:
    for sidecar in SQLITE_SIDECARS:
        _require(
            not (source / sidecar).exists(),
            "source_preflight",
            f"源库存在 SQLite 旁文件，需先停写: {source / sidecar}",
        )


def _connect_readonly(
    sqlite_path: Path,
    stage: str,
    *,
    immutable: bool,
) -> sqlite3.Connection:
    _require(
        sqlite_path.is_file(),
        stage,
        f"SQLite 文件不存在: {sqlite_path}",
    )
    connection = None
    try:
        query = "mode=ro&immutable=1" if immutable else "mode=ro"
        connection = sqlite3.connect(
            f"file:{sqlite_path.as_posix()}?{query}",
            uri=True,
        )
        connection.execute("PRAGMA query_only=ON")
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise CleanupError(stage, f"打开 SQLite 只读连接失败: {error}") from error
    return connection


def _fetch_rows(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[int, ...] = (),
) -> list[Row]:
    rows = connection.execute(statement, parameters).fetchall()
    return [cast(Row, tuple(row)) for row in rows]


def _as_int(cell: Cell) -> int:
    if isinstance(cell, int):
        return cell
    if isinstance(cell, str):
        return int(cell)
    raise ValueError("SQLite 结果缺少整数")


def _as_optional_int(cell: Cell) -> int | None:
    return None if cell is None else _as_int(cell)


def _read_state(
    directory: Path,
    stage: str,
    *,
    immutable: bool = False,
) -> EvaluationState:
    with closing(
        _connect_readonly(
            directory / APP_DB,
            stage,
            immutable=immutable,
        )
    ) as app_db:
        integrity = app_db.execute("PRAGMA integrity_check").fetchall()
        _require(
            integrity == [("ok",)],
            stage,
            f"SQLite integrity_check 失败: {integrity}",
        )
        violations = app_db.execute("PRAGMA foreign_key_check").fetchall()
        _require(
            not violations,
            stage,
            f"SQLite 外键检查失败: {violations[:5]}",
        )
        source_rows = frozenset(
            _fetch_rows(
                app_db,
                "SELECT id, url, canonical_url, title, content_hash, "
                "fingerprint_version, status, identity_status "
                "FROM knowledge_sources ORDER BY id",
            )
        )
        parent_rows = frozenset(
            (_as_int(row[0]), _as_int(row[1]))
            for row in _fetch_rows(
                app_db,
                "SELECT id, source_id FROM parent_chunks ORDER BY id",
            )
        )
        child_rows = frozenset(
            (_as_int(row[0]), _as_int(row[1]))
            for row in _fetch_rows(
                app_db,
                "SELECT id, source_id FROM child_chunks ORDER BY id",
            )
        )
        alias_rows = frozenset(
            _fetch_rows(
                app_db,
                "SELECT id, source_id, url, normalized_url, alias_type, "
                "created_at FROM source_aliases ORDER BY id",
            )
        )
        collection_rows = frozenset(
            _fetch_rows(
                app_db,
                "SELECT collection_id, source_id, added_at "
                "FROM knowledge_collection_sources "
                "ORDER BY collection_id, source_id",
            )
        )
        target_tags = frozenset(
            (_as_int(row[0]), _as_int(row[1]))
            for row in _fetch_rows(
                app_db,
                "SELECT source_id, tag_id FROM source_tags "
                "WHERE source_id IN (?, ?)",
                (KEEPER_ID, DUPLICATE_ID),
            )
        )
        target_job_refs = frozenset(
            (
                _as_int(row[0]),
                _as_optional_int(row[1]),
                _as_optional_int(row[2]),
            )
            for row in _fetch_rows(
                app_db,
                "SELECT id, source_id, duplicate_of_source_id "
                "FROM ingestion_job_items "
                "WHERE source_id IN (?, ?) "
                "OR duplicate_of_source_id IN (?, ?)",
                (KEEPER_ID, DUPLICATE_ID, KEEPER_ID, DUPLICATE_ID),
            )
        )
        fts_ids = tuple(
            _as_int(row[0])
            for row in _fetch_rows(
                app_db,
                "SELECT child_id FROM child_chunks_fts ORDER BY child_id",
            )
        )
    with closing(
        _connect_readonly(
            directory / CHROMA_DB,
            stage,
            immutable=immutable,
        )
    ) as chroma_db:
        chroma_collections = frozenset(
            (str(row[0]), str(row[1]))
            for row in _fetch_rows(
                chroma_db,
                "SELECT id, name FROM collections ORDER BY id",
            )
        )
        chroma_rows = tuple(
            (
                _as_int(row[0]),
                _as_optional_int(row[1]),
            )
            for row in _fetch_rows(
                chroma_db,
                "SELECT CAST(e.embedding_id AS INTEGER), "
                "metadata.int_value "
                "FROM embeddings AS e "
                "LEFT JOIN embedding_metadata AS metadata "
                "ON metadata.id = e.id AND metadata.key = 'source_id' "
                "ORDER BY CAST(e.embedding_id AS INTEGER)",
            )
        )
    return EvaluationState(
        source_rows=source_rows,
        parent_rows=parent_rows,
        child_rows=child_rows,
        alias_rows=alias_rows,
        collection_rows=collection_rows,
        target_tags=target_tags,
        target_job_refs=target_job_refs,
        fts_ids=fts_ids,
        chroma_rows=chroma_rows,
        chroma_collections=chroma_collections,
    )


def _counts(state: EvaluationState) -> ExpectedCounts:
    return ExpectedCounts(
        sources=len(state.source_rows),
        completed_sources=sum(
            _as_int(row[0]) > 0 and row[6] == "completed"
            for row in state.source_rows
        ),
        parents=len(state.parent_rows),
        children=len(state.child_rows),
        aliases=len(state.alias_rows),
    )


def _aliases_for_source(
    state: EvaluationState,
    source_id: int,
) -> frozenset[tuple[str, str]]:
    return frozenset(
        (str(row[2]), str(row[4]))
        for row in state.alias_rows
        if _as_int(row[1]) == source_id
    )


def _ids_by_source(
    rows: frozenset[tuple[int, int]],
    source_id: int,
) -> frozenset[int]:
    return frozenset(row[0] for row in rows if row[1] == source_id)


def _expected_source_row(
    source_id: int,
    url: str,
    expectation: ExpectedCleanup,
) -> Row:
    return (
        source_id,
        url,
        url,
        expectation.title,
        expectation.content_hash,
        expectation.fingerprint_version,
        "completed",
        "legacy_unverified",
    )


def _validate_source_rows(
    state: EvaluationState,
    expectation: ExpectedCleanup,
    stage: str,
) -> None:
    rows_by_id = {_as_int(row[0]): row for row in state.source_rows}
    _require(
        rows_by_id.get(expectation.keeper_id)
        == _expected_source_row(
            expectation.keeper_id,
            expectation.keeper_url,
            expectation,
        ),
        stage,
        "source 114 元数据与审计报告不一致",
    )
    _require(
        rows_by_id.get(expectation.duplicate_id)
        == _expected_source_row(
            expectation.duplicate_id,
            expectation.duplicate_url,
            expectation,
        ),
        stage,
        "source 119 元数据与审计报告不一致",
    )


def _validate_aliases(
    state: EvaluationState,
    expectation: ExpectedCleanup,
    stage: str,
) -> None:
    _require(
        _aliases_for_source(state, expectation.keeper_id)
        == expectation.keeper_aliases,
        stage,
        "source 114 别名与审计报告不一致",
    )
    _require(
        _aliases_for_source(state, expectation.duplicate_id)
        == expectation.duplicate_aliases,
        stage,
        "source 119 别名与审计报告不一致",
    )
    for row in state.alias_rows:
        if _as_int(row[1]) in (
            expectation.keeper_id,
            expectation.duplicate_id,
        ):
            _require(
                row[2] == row[3],
                stage,
                f"别名 {row[0]} 的规范化 URL 不一致",
            )


def _parent_tree(
    connection: sqlite3.Connection,
    source_id: int,
) -> dict[int, tuple[Cell, Cell]]:
    return {
        _as_int(row[0]): (row[1], row[2])
        for row in _fetch_rows(
            connection,
            "SELECT ordinal, heading_path, content "
            "FROM parent_chunks WHERE source_id = ? ORDER BY ordinal",
            (source_id,),
        )
    }


def _child_tree(
    connection: sqlite3.Connection,
    source_id: int,
) -> dict[tuple[int, int], tuple[Cell, Cell, Cell, Cell]]:
    rows = _fetch_rows(
        connection,
        "SELECT parent.ordinal, child.ordinal, child.text, "
        "child.embedding_text, child.content_type, child.heading_path "
        "FROM child_chunks AS child "
        "JOIN parent_chunks AS parent ON parent.id = child.parent_id "
        "WHERE child.source_id = ? "
        "ORDER BY parent.ordinal, child.ordinal",
        (source_id,),
    )
    return {
        (_as_int(row[0]), _as_int(row[1])): (
            row[2],
            row[3],
            row[4],
            row[5],
        )
        for row in rows
    }


def _validate_chunk_trees(
    directory: Path,
    state: EvaluationState,
    expectation: ExpectedCleanup,
    stage: str,
) -> None:
    _require(
        _ids_by_source(state.parent_rows, expectation.keeper_id)
        == expectation.keeper_parent_ids,
        stage,
        "source 114 Parent ID 与审计报告不一致",
    )
    _require(
        _ids_by_source(state.parent_rows, expectation.duplicate_id)
        == expectation.duplicate_parent_ids,
        stage,
        "source 119 Parent ID 与审计报告不一致",
    )
    _require(
        _ids_by_source(state.child_rows, expectation.keeper_id)
        == expectation.keeper_child_ids,
        stage,
        "source 114 Child ID 与审计报告不一致",
    )
    _require(
        _ids_by_source(state.child_rows, expectation.duplicate_id)
        == expectation.duplicate_child_ids,
        stage,
        "source 119 Child ID 与审计报告不一致",
    )
    with closing(
        _connect_readonly(
            directory / APP_DB,
            stage,
            immutable=True,
        )
    ) as connection:
        _require(
            _parent_tree(connection, expectation.keeper_id)
            == _parent_tree(connection, expectation.duplicate_id),
            stage,
            "source 114 与 119 的 Parent 正文树不一致",
        )
        _require(
            _child_tree(connection, expectation.keeper_id)
            == _child_tree(connection, expectation.duplicate_id),
            stage,
            "source 114 与 119 的 Child 正文树不一致",
        )


def _validate_indexes(
    state: EvaluationState,
    expectation: ExpectedCleanup,
    stage: str,
) -> None:
    db_sources = {
        child_id: source_id for child_id, source_id in state.child_rows
    }
    db_ids = set(db_sources)
    _require(
        len(state.fts_ids) == len(db_ids) and set(state.fts_ids) == db_ids,
        stage,
        "SQLite Child 与 FTS5 ID 集合不一致或含重复",
    )
    chroma_sources = {
        child_id: source_id for child_id, source_id in state.chroma_rows
    }
    _require(
        len(state.chroma_rows) == len(db_ids)
        and set(chroma_sources) == db_ids,
        stage,
        "SQLite Child 与 Chroma ID 集合不一致或含重复",
    )
    _require(
        chroma_sources == db_sources,
        stage,
        "Chroma source_id 元数据与 SQLite 不一致",
    )
    duplicate_chroma_ids = {
        child_id
        for child_id, source_id in chroma_sources.items()
        if source_id == expectation.duplicate_id
    }
    _require(
        duplicate_chroma_ids == expectation.duplicate_child_ids,
        stage,
        "source 119 Chroma 47 个 ID 与审计报告不一致",
    )
    _require(
        len(state.chroma_collections) == 1
        and next(iter(state.chroma_collections))[1] == "child_chunks",
        stage,
        "Chroma collection 集合与审计报告不一致",
    )


def _validate_initial_state(
    directory: Path,
    state: EvaluationState,
    expectation: ExpectedCleanup,
    stage: str,
) -> None:
    _require(
        _counts(state) == expectation.counts,
        stage,
        f"全局行数与审计报告不一致: {_counts(state)}",
    )
    _validate_source_rows(state, expectation, stage)
    _validate_aliases(state, expectation, stage)
    _validate_chunk_trees(directory, state, expectation, stage)
    _validate_indexes(state, expectation, stage)
    memberships = frozenset(
        (str(row[0]), _as_int(row[1]))
        for row in state.collection_rows
        if _as_int(row[1])
        in (expectation.keeper_id, expectation.duplicate_id)
    )
    _require(
        memberships == expectation.collection_memberships,
        stage,
        "source 114/119 的知识集合关联不符",
    )
    _require(not state.target_tags, stage, "source 114/119 存在未预期标签")
    _require(
        not state.target_job_refs,
        stage,
        "source 114/119 存在未预期入库任务引用",
    )


def _expected_after_state(
    before: EvaluationState,
    expectation: ExpectedCleanup,
) -> EvaluationState:
    aliases = frozenset(
        (row[0], expectation.keeper_id, *row[2:])
        if _as_int(row[1]) == expectation.duplicate_id
        else row
        for row in before.alias_rows
    )
    return replace(
        before,
        source_rows=frozenset(
            row
            for row in before.source_rows
            if _as_int(row[0]) != expectation.duplicate_id
        ),
        parent_rows=frozenset(
            row
            for row in before.parent_rows
            if row[0] not in expectation.duplicate_parent_ids
        ),
        child_rows=frozenset(
            row
            for row in before.child_rows
            if row[0] not in expectation.duplicate_child_ids
        ),
        alias_rows=aliases,
        collection_rows=frozenset(
            row
            for row in before.collection_rows
            if _as_int(row[1]) != expectation.duplicate_id
        ),
        fts_ids=tuple(
            child_id
            for child_id in before.fts_ids
            if child_id not in expectation.duplicate_child_ids
        ),
        chroma_rows=tuple(
            row
            for row in before.chroma_rows
            if row[0] not in expectation.duplicate_child_ids
        ),
    )


def _first_mismatch(
    actual: EvaluationState,
    expected: EvaluationState,
) -> str:
    for state_field in fields(EvaluationState):
        actual_field = getattr(actual, state_field.name)
        expected_field = getattr(expected, state_field.name)
        if actual_field == expected_field:
            continue
        if isinstance(actual_field, frozenset | tuple):
            actual_set = set(actual_field)
            expected_set = set(expected_field)
            missing = sorted(
                repr(record) for record in expected_set - actual_set
            )[:5]
            extra = sorted(
                repr(record) for record in actual_set - expected_set
            )[:5]
            return f"{state_field.name} 不一致，缺少={missing}，多出={extra}"
        return f"{state_field.name} 不一致"
    return "状态不一致"


def _validate_cleaned_state(
    state: EvaluationState,
    expected: EvaluationState,
    expectation: ExpectedCleanup,
) -> None:
    _require(
        state == expected,
        "post_verification",
        _first_mismatch(state, expected),
    )
    _require(
        _aliases_for_source(state, expectation.keeper_id)
        == expectation.keeper_aliases | expectation.duplicate_aliases,
        "post_verification",
        "source 119 URL 未完整归属 source 114",
    )


def _copy_to_new_target(source: Path, target: Path) -> None:
    try:
        shutil.copytree(source, target, copy_function=shutil.copy2)
    except OSError as error:
        raise CleanupError("copy", f"创建独立副本失败: {error}") from error


def _apply_cleanup(target: Path, expectation: ExpectedCleanup) -> None:
    engine = None
    vector = None
    try:
        engine = create_db_engine(target / APP_DB)
        session_factory = create_session_factory(engine)
        vector = VectorIndex(target / CHROMA_DIR)
        identity = SourceIdentityService(session_factory=session_factory)
        source_service = SourceService(
            session_factory=session_factory,
            vector_index=vector,
            fts_index=FtsIndex(engine),
        )
        try:
            identity.merge_aliases(
                from_source_id=expectation.duplicate_id,
                to_source_id=expectation.keeper_id,
            )
        except Exception as error:
            raise CleanupError(
                "alias_merge",
                f"别名迁移失败: {type(error).__name__}: {error}",
            ) from error
        try:
            source_service.delete(expectation.duplicate_id)
        except Exception as error:
            raise CleanupError(
                "source_delete",
                f"来源删除失败: {type(error).__name__}: {error}",
            ) from error
    finally:
        if vector is not None:
            vector.close()
        if engine is not None:
            engine.dispose()


def _verify_chroma_ids(
    target: Path,
    expected_all_child_ids: frozenset[int],
) -> None:
    vector = None
    try:
        vector = VectorIndex(target / CHROMA_DIR)
        _require(
            vector.count() == len(expected_all_child_ids),
            "post_verification",
            "重新打开 Chroma 后向量数量不符",
        )
        _require(
            set(vector.all_ids()) == expected_all_child_ids,
            "post_verification",
            "重新打开 Chroma 后 Child ID 集合不符",
        )
    except CleanupError:
        raise
    except Exception as error:
        raise CleanupError(
            "post_verification",
            f"重新打开 Chroma 客户端失败: {type(error).__name__}: {error}",
        ) from error
    finally:
        if vector is not None:
            vector.close()


def _counts_payload(counts: ExpectedCounts) -> dict[str, int]:
    return {
        "sources": counts.sources,
        "completed_sources": counts.completed_sources,
        "parents": counts.parents,
        "children": counts.children,
        "aliases": counts.aliases,
    }


def _build_report(
    source: Path,
    target: Path,
    before: EvaluationState,
    after: EvaluationState,
    source_hashes_before: dict[str, str],
    source_hashes_after: dict[str, str],
    expectation: ExpectedCleanup,
) -> dict[str, object]:
    return {
        "status": "success",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_dir": str(source),
        "target_dir": str(target),
        "keeper_source_id": expectation.keeper_id,
        "removed_source_id": expectation.duplicate_id,
        "counts_before": _counts_payload(_counts(before)),
        "counts_after": _counts_payload(_counts(after)),
        "keeper_parent_ids": sorted(
            parent_id
            for parent_id, source_id in after.parent_rows
            if source_id == expectation.keeper_id
        ),
        "keeper_child_ids": sorted(
            child_id
            for child_id, source_id in after.child_rows
            if source_id == expectation.keeper_id
        ),
        "removed_parent_ids": sorted(
            parent_id
            for parent_id, source_id in before.parent_rows
            if source_id == expectation.duplicate_id
        ),
        "removed_child_ids": sorted(
            child_id
            for child_id, source_id in before.child_rows
            if source_id == expectation.duplicate_id
        ),
        "keeper_aliases": sorted(
            _aliases_for_source(after, expectation.keeper_id)
        ),
        "source_sha256_before": source_hashes_before,
        "source_sha256_after": source_hashes_after,
        "original_unchanged": source_hashes_before == source_hashes_after,
        "validated_stages": [
            "source_preflight",
            "copy_integrity",
            "copy_preflight",
            "alias_merge",
            "source_delete",
            "post_verification",
            "source_integrity",
        ],
    }


def _write_report(target: Path, report: dict[str, object]) -> Path:
    report_path = target / REPORT_FILE
    try:
        with report_path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except OSError as error:
        raise CleanupError("report", f"写入清理报告失败: {error}") from error
    return report_path


def run_cleanup(
    options: CleanupOptions,
    expectation: ExpectedCleanup = DEFAULT_EXPECTATION,
) -> dict[str, object]:
    """执行一次受守卫的副本清理；失败后不会继续或重试。"""

    _require(
        options.confirm_cleanup,
        "confirmation",
        "缺少显式确认参数，拒绝执行",
    )
    source, target = _resolve_clean_paths(options)
    _assert_source_quiescent(source)
    source_hashes_before = _hash_tree(source, "source_preflight")
    try:
        before = _read_state(
            source,
            "source_preflight",
            immutable=True,
        )
        _validate_initial_state(
            source,
            before,
            expectation,
            "source_preflight",
        )
    except CleanupError:
        raise
    except Exception as error:
        raise CleanupError(
            "source_preflight",
            f"原库预检失败: {type(error).__name__}: {error}",
        ) from error

    _copy_to_new_target(source, target)
    _require(
        _hash_tree(target, "copy_integrity") == source_hashes_before,
        "copy_integrity",
        "副本文件哈希与源目录不一致",
    )
    _require(
        _hash_tree(source, "source_integrity") == source_hashes_before,
        "source_integrity",
        "复制期间原库发生变化",
    )
    try:
        copy_before = _read_state(
            target,
            "copy_preflight",
            immutable=True,
        )
        _validate_initial_state(
            target,
            copy_before,
            expectation,
            "copy_preflight",
        )
        _require(
            copy_before == before,
            "copy_preflight",
            "副本状态与原库预检快照不一致",
        )
    except CleanupError:
        raise
    except Exception as error:
        raise CleanupError(
            "copy_preflight",
            f"副本预检失败: {type(error).__name__}: {error}",
        ) from error

    try:
        _apply_cleanup(target, expectation)
    except CleanupError:
        raise
    except Exception as error:
        raise CleanupError(
            "cleanup_client",
            f"初始化或关闭清理客户端失败: "
            f"{type(error).__name__}: {error}",
        ) from error
    try:
        after = _read_state(target, "post_verification")
        expected_after = _expected_after_state(before, expectation)
        _validate_cleaned_state(after, expected_after, expectation)
        _verify_chroma_ids(
            target,
            frozenset(
                child_id for child_id, _source_id in after.child_rows
            ),
        )
    except CleanupError:
        raise
    except Exception as error:
        raise CleanupError(
            "post_verification",
            f"清理后核验失败: {type(error).__name__}: {error}",
        ) from error

    source_hashes_after = _hash_tree(source, "source_integrity")
    _require(
        source_hashes_after == source_hashes_before,
        "source_integrity",
        "执行期间原库文件哈希发生变化",
    )
    report = _build_report(
        source,
        target,
        before,
        after,
        source_hashes_before,
        source_hashes_after,
        expectation,
    )
    report_path = _write_report(target, report)
    return {**report, "report_file": str(report_path)}


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "在全新独立副本中将 source 119 合并到 114；"
            "未显式确认时拒绝执行。"
        )
    )
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument(
        "--confirm-cleanup",
        action="store_true",
        help="显式确认创建副本并执行清理",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    options = CleanupOptions(
        source_dir=args.source_dir,
        target_dir=args.target_dir,
        confirm_cleanup=args.confirm_cleanup,
    )
    try:
        report = run_cleanup(options)
    except CleanupError as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "stage": error.stage,
                    "reason": error.reason,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
