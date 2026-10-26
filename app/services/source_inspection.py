"""来源正文与切块的只读预览。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import AppError, ErrorCode
from app.domain.state import SourceStatus
from app.infra.db import (
    ChildChunk,
    KnowledgeSource,
    ParentChunk,
    SourceAlias,
)


@dataclass(frozen=True)
class SourceAliasView:
    url: str
    normalized_url: str
    alias_type: str
    created_at: str


@dataclass(frozen=True)
class ChildView:
    id: int
    ordinal: int
    heading_path: str
    text: str
    content_type: str


@dataclass(frozen=True)
class ParentView:
    id: int
    ordinal: int
    heading_path: str
    content: str
    children: tuple[ChildView, ...]


@dataclass(frozen=True)
class SourceContentView:
    source_id: int
    submitted_url: str
    final_url: str
    title: str | None
    status: str
    content_hash: str | None
    fingerprint_version: str | None
    language_code: str | None
    identity_status: str
    identity_verified_at: str | None
    created_at: str
    updated_at: str
    processed_at: str | None
    last_checked_at: str | None
    aliases: tuple[SourceAliasView, ...]
    parent_total: int
    child_total: int
    page: int
    size: int
    parents: tuple[ParentView, ...]


class SourceInspectionService:
    """从 SQLite 权威数据读取 completed 来源的正文树。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        parent_limit: int,
    ) -> None:
        if parent_limit < 1:
            raise ValueError("parent_limit 必须为正整数")
        self._session_factory = session_factory
        self._parent_limit = parent_limit

    def get_content(
        self,
        source_id: int,
        *,
        page: int,
        size: int,
    ) -> SourceContentView:
        """分页返回来源元数据、关系标签与 Parent/Child 正文树。"""

        self._validate_pagination(page, size)
        with self._session_factory() as session:
            source = self._completed_source(session, source_id)
            parent_total = self._count_parents(session, source_id)
            child_total = self._count_children(session, source_id)
            if parent_total == 0 or child_total == 0:
                raise AppError(
                    ErrorCode.SOURCE_CONTENT_NOT_READY,
                    "来源正文数据不完整",
                )
            parents = self._read_parents(
                session,
                source_id=source_id,
                page=page,
                size=size,
            )
            return SourceContentView(
                source_id=source.id,
                submitted_url=source.url,
                final_url=source.canonical_url,
                title=source.title,
                status=source.status,
                content_hash=source.content_hash,
                fingerprint_version=source.fingerprint_version,
                language_code=source.language_code,
                identity_status=source.identity_status,
                identity_verified_at=source.identity_verified_at,
                created_at=source.created_at,
                updated_at=source.updated_at,
                processed_at=source.processed_at,
                last_checked_at=source.last_checked_at,
                aliases=self._read_aliases(session, source_id),
                parent_total=parent_total,
                child_total=child_total,
                page=page,
                size=size,
                parents=parents,
            )

    def _validate_pagination(self, page: int, size: int) -> None:
        if page < 1 or size < 1 or size > self._parent_limit:
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                f"page 必须为正整数，size 必须在 1 到 {self._parent_limit} 之间",
            )

    @staticmethod
    def _completed_source(
        session: Session,
        source_id: int,
    ) -> KnowledgeSource:
        source = session.get(KnowledgeSource, source_id)
        if source is None:
            raise AppError(ErrorCode.NOT_FOUND, f"来源不存在: {source_id}")
        if source.status != SourceStatus.COMPLETED.value:
            raise AppError(
                ErrorCode.SOURCE_CONTENT_NOT_READY,
                "来源尚未完成，无法预览正文",
            )
        return source

    @staticmethod
    def _count_parents(session: Session, source_id: int) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(ParentChunk)
                .where(ParentChunk.source_id == source_id)
            )
            or 0
        )

    @staticmethod
    def _count_children(session: Session, source_id: int) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(ChildChunk)
                .where(ChildChunk.source_id == source_id)
            )
            or 0
        )

    @staticmethod
    def _read_aliases(
        session: Session,
        source_id: int,
    ) -> tuple[SourceAliasView, ...]:
        rows = session.scalars(
            select(SourceAlias)
            .where(SourceAlias.source_id == source_id)
            .order_by(SourceAlias.created_at, SourceAlias.id)
        ).all()
        return tuple(
            SourceAliasView(
                url=row.url,
                normalized_url=row.normalized_url,
                alias_type=row.alias_type,
                created_at=row.created_at,
            )
            for row in rows
        )

    def _read_parents(
        self,
        session: Session,
        *,
        source_id: int,
        page: int,
        size: int,
    ) -> tuple[ParentView, ...]:
        parent_rows = session.scalars(
            select(ParentChunk)
            .where(ParentChunk.source_id == source_id)
            .order_by(ParentChunk.ordinal, ParentChunk.id)
            .limit(size)
            .offset((page - 1) * size)
        ).all()
        parent_ids = [parent.id for parent in parent_rows]
        children_by_parent = self._read_children_by_parent(
            session,
            source_id=source_id,
            parent_ids=parent_ids,
        )
        return tuple(
            self._parent_view(parent, children_by_parent[parent.id])
            for parent in parent_rows
        )

    @staticmethod
    def _read_children_by_parent(
        session: Session,
        *,
        source_id: int,
        parent_ids: Sequence[int],
    ) -> dict[int, list[ChildView]]:
        children_by_parent: dict[int, list[ChildView]] = {
            parent_id: [] for parent_id in parent_ids
        }
        if not parent_ids:
            return children_by_parent
        child_rows = session.scalars(
            select(ChildChunk)
            .where(
                ChildChunk.source_id == source_id,
                ChildChunk.parent_id.in_(parent_ids),
            )
            .order_by(
                ChildChunk.parent_id,
                ChildChunk.ordinal,
                ChildChunk.id,
            )
        ).all()
        for child in child_rows:
            children_by_parent[child.parent_id].append(
                ChildView(
                    id=child.id,
                    ordinal=child.ordinal,
                    heading_path=child.heading_path,
                    text=child.text,
                    content_type=child.content_type,
                )
            )
        return children_by_parent

    @staticmethod
    def _parent_view(
        parent: ParentChunk,
        children: list[ChildView],
    ) -> ParentView:
        return ParentView(
            id=parent.id,
            ordinal=parent.ordinal,
            heading_path=parent.heading_path,
            content=parent.content,
            children=tuple(children),
        )
