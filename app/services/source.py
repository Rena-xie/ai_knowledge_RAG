"""来源管理与级联删除（见《项目开发说明_v1.md》5.3、5.5）。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import utc_now_iso
from app.core.errors import AppError, ErrorCode
from app.domain.state import SourceStatus, allows_direct_delete, can_transition
from app.domain.url import InvalidUrlError, normalize_url
from app.infra.db import ChildChunk, KnowledgeSource, ParentChunk, SourceAlias
from app.infra.fts import FtsIndex
from app.infra.vector import VectorIndex

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubmitResult:
    """提交结果：来源记录与是否触发入库。"""

    source: KnowledgeSource
    triggered: bool


class SourceService:
    """来源记录的新增、查询、分页与级联删除。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        vector_index: VectorIndex,
        fts_index: FtsIndex,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self._session_factory = session_factory
        self._vector_index = vector_index
        self._fts_index = fts_index
        self._clock = clock

    def get_or_create(self, url: str) -> SubmitResult:
        """按规范化 URL 鉴重；重复提交行为见设计文档 5.3。"""

        try:
            canonical = normalize_url(url)
        except InvalidUrlError as error:
            raise AppError(ErrorCode.INVALID_REQUEST, str(error)) from error
        with self._session_factory() as session:
            existing = self._find_by_canonical(session, canonical)
            if existing is not None:
                return self._existing_result(existing)
            now = self._clock()
            source = KnowledgeSource(
                url=url,
                canonical_url=canonical,
                status=SourceStatus.PENDING.value,
                identity_status="pending",
                created_at=now,
                updated_at=now,
            )
            session.add(source)
            try:
                session.commit()
            except IntegrityError:
                # 并发提交同一 URL：canonical_url 唯一约束兜底，回查既有记录后
                # 按其状态返回，不向调用方抛出 5xx（见 5.3 第 3 点）。
                session.rollback()
                existing = self._find_by_canonical(session, canonical)
                if existing is None:
                    raise
                return self._existing_result(existing)
            return SubmitResult(source=source, triggered=True)

    @staticmethod
    def _find_by_canonical(session: Session, canonical: str) -> KnowledgeSource | None:
        direct = session.scalar(
            select(KnowledgeSource).where(KnowledgeSource.canonical_url == canonical)
        )
        if direct is not None:
            return direct
        alias = session.scalar(
            select(SourceAlias).where(SourceAlias.normalized_url == canonical)
        )
        if alias is None:
            return None
        return session.get(KnowledgeSource, alias.source_id)

    @staticmethod
    def _existing_result(source: KnowledgeSource) -> SubmitResult:
        return SubmitResult(
            source=source,
            triggered=source.status == SourceStatus.FAILED.value,
        )

    def get(self, source_id: int) -> KnowledgeSource | None:
        with self._session_factory() as session:
            return session.get(KnowledgeSource, source_id)

    def discard_unindexed_source(self, source_id: int) -> None:
        """删除去重命中后被合并的临时来源。"""

        with self._session_factory() as session:
            source = session.get(KnowledgeSource, source_id)
            if source is None:
                return
            child_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(ChildChunk)
                    .where(ChildChunk.source_id == source_id)
                )
                or 0
            )
            if child_count:
                raise RuntimeError(
                    f"refusing to discard indexed source {source_id}"
                )
            session.delete(source)
            session.commit()

    def list_sources(
        self, *, page: int, size: int
    ) -> tuple[list[KnowledgeSource], int]:
        if page < 1 or size < 1:
            raise AppError(ErrorCode.INVALID_REQUEST, "page 与 size 必须为正整数")
        with self._session_factory() as session:
            total = int(
                session.scalar(select(func.count()).select_from(KnowledgeSource)) or 0
            )
            rows = session.scalars(
                select(KnowledgeSource)
                .order_by(KnowledgeSource.id.desc())
                .limit(size)
                .offset((page - 1) * size)
            ).all()
            return list(rows), total

    def delete(self, source_id: int) -> None:
        """按 5.5 的顺序级联删除；失败保留 deleting 状态并可重入重试。"""

        with self._session_factory() as session:
            source = session.get(KnowledgeSource, source_id)
            if source is None:
                raise AppError(ErrorCode.NOT_FOUND, f"来源不存在: {source_id}")
            status = SourceStatus(source.status)
            if allows_direct_delete(status):
                session.delete(source)
                session.commit()
                return
            if status is not SourceStatus.DELETING:
                if not can_transition(status, SourceStatus.DELETING):
                    raise AppError(
                        ErrorCode.INVALID_REQUEST,
                        f"当前状态不允许删除: {status.value}",
                    )
                source.status = SourceStatus.DELETING.value
                source.updated_at = self._clock()
                session.commit()

        child_ids = self._child_ids(source_id)
        stage = "index"
        try:
            self._vector_index.delete_by_source(source_id)
            self._fts_index.delete_many(child_ids)
            stage = "db"
            with self._session_factory() as session:
                session.execute(
                    delete(ChildChunk).where(ChildChunk.source_id == source_id)
                )
                session.execute(
                    delete(ParentChunk).where(ParentChunk.source_id == source_id)
                )
                session.execute(
                    delete(KnowledgeSource).where(KnowledgeSource.id == source_id)
                )
                session.commit()
        except AppError as error:
            # 索引层已给出明确错误码（如 INDEX_WRITE_FAILED），原样保留。
            self._mark_delete_failed(source_id, error.code, error.message)
            raise AppError(error.code, f"级联删除失败: {error}") from error
        except Exception as error:
            # 按失败阶段区分：关系数据删除失败是数据库故障，派生索引删除失败
            # 才是索引写入失败，避免排查时被错误码引向错误方向。
            code = ErrorCode.DB_ERROR if stage == "db" else ErrorCode.INDEX_WRITE_FAILED
            self._mark_delete_failed(source_id, code, str(error))
            raise AppError(code, f"级联删除失败: {error}") from error

    def mark_status(
        self,
        source_id: int,
        status: SourceStatus,
        *,
        error_code: ErrorCode | None = None,
        error_message: str | None = None,
    ) -> None:
        """更新来源状态与错误信息（供入库流水线调用）。"""

        with self._session_factory() as session:
            source = session.get(KnowledgeSource, source_id)
            if source is None:
                raise AppError(ErrorCode.NOT_FOUND, f"来源不存在: {source_id}")
            current = SourceStatus(source.status)
            if current is not status and not can_transition(current, status):
                raise AppError(
                    ErrorCode.INVALID_REQUEST,
                    f"非法状态迁移: {current.value} -> {status.value}",
                )
            source.status = status.value
            source.error_code = error_code.value if error_code else None
            source.error_message = error_message
            source.updated_at = self._clock()
            session.commit()

    def mark_checked(self, source_id: int) -> None:
        """记录一次刷新检查时间，不改变来源状态。"""

        with self._session_factory() as session:
            source = session.get(KnowledgeSource, source_id)
            if source is None:
                raise AppError(ErrorCode.NOT_FOUND, f"来源不存在: {source_id}")
            now = self._clock()
            source.last_checked_at = now
            source.updated_at = now
            session.commit()

    def _child_ids(self, source_id: int) -> list[int]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(ChildChunk.id).where(ChildChunk.source_id == source_id)
                ).all()
            )

    def _mark_delete_failed(
        self, source_id: int, code: ErrorCode, message: str
    ) -> None:
        try:
            with self._session_factory() as session:
                source = session.get(KnowledgeSource, source_id)
                if source is not None:
                    source.error_code = code.value
                    source.error_message = message
                    source.updated_at = self._clock()
                    session.commit()
        except Exception:
            logger.warning(
                "记录删除失败状态时出错",
                extra={"source_id": source_id, "error_code": code.value},
            )
