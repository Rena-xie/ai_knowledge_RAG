"""入库流水线（见《项目开发说明_v1.md》4.4、5.3、5.4）。"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import utc_now_iso
from app.core.errors import AppError, ErrorCode
from app.core.settings import Settings
from app.domain.chunking import ParentChunk as DomainParent
from app.domain.chunking import chunk_markdown
from app.domain.state import SourceStatus
from app.infra.db import ChildChunk, KnowledgeSource, ParentChunk
from app.infra.embedding import EmbeddingClient, EmbeddingError
from app.infra.fetcher import Fetcher, FetchError
from app.infra.fts import FtsIndex
from app.infra.parser import ParsedDocument, ParseError, parse_html
from app.infra.vector import VectorIndex
from app.services.source import SourceService

logger = logging.getLogger(__name__)

DEFAULT_CHUNKER_VERSION = "v1"


@dataclass(frozen=True)
class IngestResult:
    """入库结果。"""

    source_id: int
    status: str
    triggered: bool
    child_count: int
    changed: bool = True


class IngestionService:
    """执行采集 → 解析 → 切块 → 向量化 → 双索引写入，并处理补偿清理。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        source_service: SourceService,
        fetcher: Fetcher,
        embedder: EmbeddingClient,
        fts_index: FtsIndex,
        vector_index: VectorIndex,
        settings: Settings,
        parser: Callable[..., ParsedDocument] = parse_html,
        clock: Callable[[], str] = utc_now_iso,
        chunker_version: str = DEFAULT_CHUNKER_VERSION,
    ) -> None:
        self._session_factory = session_factory
        self._source_service = source_service
        self._fetcher = fetcher
        self._embedder = embedder
        self._fts_index = fts_index
        self._vector_index = vector_index
        self._settings = settings
        self._parser = parser
        self._clock = clock
        self._chunker_version = chunker_version
        self._lock = threading.Lock()

    def ingest(self, url: str, *, force_refresh: bool = False) -> IngestResult:
        submit = self._source_service.get_or_create(url)
        source_id = submit.source.id
        if not submit.triggered:
            if (
                force_refresh
                and str(submit.source.status) == SourceStatus.COMPLETED.value
            ):
                if not self._lock.acquire(
                    timeout=self._settings.ingest_lock_timeout_s
                ):
                    raise AppError(
                        ErrorCode.INGEST_BUSY, "已有入库任务在执行"
                    )
                try:
                    return self._refresh(source_id, url)
                finally:
                    self._lock.release()
            return IngestResult(
                source_id=source_id,
                status=str(submit.source.status),
                triggered=False,
                child_count=self._child_count(source_id),
            )
        if not self._lock.acquire(timeout=self._settings.ingest_lock_timeout_s):
            self._mark_ingest_busy(source_id)
            raise AppError(ErrorCode.INGEST_BUSY, "已有入库任务在执行")
        try:
            return self._run_pipeline(source_id, url)
        finally:
            self._lock.release()

    def recover_interrupted(self) -> tuple[int, ...]:
        """启动时回收中断的入库记录（见《项目开发说明_v1.md》10.6 第 4 条）。

        单进程约束下启动时不存在正在执行的入库任务，遗留的 `pending` 与
        `processing` 记录已无人处理，标记为 `failed` 后可通过重新提交重试。
        """

        with self._session_factory() as session:
            source_ids = list(
                session.scalars(
                    select(KnowledgeSource.id).where(
                        KnowledgeSource.status.in_(
                            [
                                SourceStatus.PENDING.value,
                                SourceStatus.PROCESSING.value,
                            ]
                        )
                    )
                ).all()
            )
        for source_id in source_ids:
            self._source_service.mark_status(
                source_id,
                SourceStatus.FAILED,
                error_code=ErrorCode.INGEST_INTERRUPTED,
                error_message="服务重启导致入库中断，重新提交同一 URL 可重试",
            )
        return tuple(source_ids)

    def _mark_ingest_busy(self, source_id: int) -> None:
        """抢锁失败时回收本次记录，避免留下无人处理的 `pending` 来源。"""

        try:
            self._source_service.mark_status(
                source_id,
                SourceStatus.FAILED,
                error_code=ErrorCode.INGEST_BUSY,
                error_message="已有入库任务在执行",
            )
        except Exception:
            logger.warning(
                "标记入库忙状态失败",
                extra={"source_id": source_id, "stage": "ingest_lock"},
            )

    def _refresh(self, source_id: int, url: str) -> IngestResult:
        """刷新已完成来源：先完成外部工作，再在事务内替换数据。

        任何抓取/解析/切块/Embedding 失败都不会触碰旧数据；索引写入失败会
        回滚关系数据并清理新索引，旧索引保持不变。
        """

        deadline = time.monotonic() + self._settings.ingest_timeout_s
        try:
            fetched = self._fetcher.fetch(url)
            self._check_deadline(deadline)
            document = self._parser(fetched.html, url=fetched.url)
            new_hash = hashlib.sha256(
                document.markdown.encode("utf-8")
            ).hexdigest()
            current = self._source_service.get(source_id)
            if current is None:
                raise AppError(
                    ErrorCode.NOT_FOUND, f"来源不存在: {source_id}"
                )
            if current.content_hash == new_hash:
                self._source_service.mark_checked(source_id)
                return IngestResult(
                    source_id=source_id,
                    status=SourceStatus.COMPLETED.value,
                    triggered=True,
                    child_count=self._child_count(source_id),
                    changed=False,
                )
            parents = chunk_markdown(
                document.markdown,
                parent_max_chars=self._settings.parent_max_chars,
                child_target_chars=self._settings.child_target_chars,
                child_max_chars=self._settings.child_max_chars,
                child_overlap_chars=self._settings.child_overlap_chars,
                fallback_heading_path=document.title,
            )
            if not parents:
                raise AppError(ErrorCode.PARSE_EMPTY_CONTENT, "切块结果为空")
            self._check_deadline(deadline)
            embedding_texts = [
                child.embedding_text
                for parent in parents
                for child in parent.children
            ]
            embeddings = self._embedder.embed(
                embedding_texts,
                budget_s=deadline - time.monotonic(),
            )
            self._check_deadline(deadline)

            old_child_ids = self._child_ids(source_id)
            parent_max, child_max = self._max_parent_child_ids()
            new_child_ids: list[int] = []
            try:
                with self._session_factory() as session:
                    try:
                        session.execute(
                            delete(ChildChunk).where(
                                ChildChunk.source_id == source_id
                            )
                        )
                        session.execute(
                            delete(ParentChunk).where(
                                ParentChunk.source_id == source_id
                            )
                        )
                        session.flush()
                        new_child_ids, inserted_texts = self._insert_chunks(
                            session,
                            source_id,
                            parents,
                            start_parent_id=parent_max + 1,
                            start_child_id=child_max + 1,
                        )
                        fts_connection = session.connection()
                        for child_id, text in zip(
                            new_child_ids, inserted_texts, strict=True
                        ):
                            self._fts_index.upsert(
                                child_id, text, connection=fts_connection
                            )
                        self._vector_index.upsert(
                            new_child_ids,
                            embeddings,
                            [
                                {"source_id": source_id}
                                for _ in new_child_ids
                            ],
                        )
                        self._apply_source_success(
                            session, source_id, document, new_hash
                        )
                        session.commit()
                    except Exception:
                        session.rollback()
                        raise
            except Exception:
                self._delete_child_indexes(new_child_ids)
                raise

            # 新数据已提交；旧索引清理失败不影响本次刷新结果，可重建清理。
            self._delete_child_indexes(old_child_ids)
            return IngestResult(
                source_id=source_id,
                status=SourceStatus.COMPLETED.value,
                triggered=True,
                child_count=len(new_child_ids),
                changed=True,
            )
        except AppError:
            raise
        except Exception as error:
            raise AppError(_error_code(error), str(error)) from error

    def _run_pipeline(self, source_id: int, url: str) -> IngestResult:
        deadline = time.monotonic() + self._settings.ingest_timeout_s
        try:
            self._source_service.mark_status(source_id, SourceStatus.PROCESSING)
            stage = time.perf_counter()
            fetched = self._fetcher.fetch(url)
            self._log_stage(source_id, "fetch", stage)
            self._check_deadline(deadline)
            stage = time.perf_counter()
            document = self._parser(fetched.html, url=fetched.url)
            self._log_stage(source_id, "parse", stage)
            return self._run_document_pipeline(source_id, document, deadline)
        except Exception as error:
            code = _error_code(error)
            self._compensate(source_id)
            self._source_service.mark_status(
                source_id,
                SourceStatus.FAILED,
                error_code=code,
                error_message=str(error),
            )
            raise AppError(code, str(error)) from error

    def ingest_markdown(
        self,
        url: str,
        markdown: str,
        *,
        title: str | None = None,
    ) -> IngestResult:
        """从已清洗的 Markdown 入库（用于官方文档仓库等无 HTML 的来源）。"""

        submit = self._source_service.get_or_create(url)
        source_id = submit.source.id
        if not submit.triggered:
            return IngestResult(
                source_id=source_id,
                status=str(submit.source.status),
                triggered=False,
                child_count=self._child_count(source_id),
            )
        if not self._lock.acquire(timeout=self._settings.ingest_lock_timeout_s):
            self._mark_ingest_busy(source_id)
            raise AppError(ErrorCode.INGEST_BUSY, "已有入库任务在执行")
        try:
            self._source_service.mark_status(
                source_id, SourceStatus.PROCESSING
            )
            text = (markdown or "").strip()
            if not text:
                raise AppError(
                    ErrorCode.PARSE_EMPTY_CONTENT, "Markdown 正文为空"
                )
            document = ParsedDocument(
                title=title or _markdown_title(text),
                markdown=text,
            )
            deadline = (
                time.monotonic() + self._settings.ingest_timeout_s
            )
            return self._run_document_pipeline(
                source_id, document, deadline
            )
        except Exception as error:
            code = _error_code(error)
            self._compensate(source_id)
            self._source_service.mark_status(
                source_id,
                SourceStatus.FAILED,
                error_code=code,
                error_message=str(error),
            )
            raise AppError(code, str(error)) from error
        finally:
            self._lock.release()

    def _run_document_pipeline(
        self,
        source_id: int,
        document: ParsedDocument,
        deadline: float,
    ) -> IngestResult:
        stage = time.perf_counter()
        parents = chunk_markdown(
            document.markdown,
            parent_max_chars=self._settings.parent_max_chars,
            child_target_chars=self._settings.child_target_chars,
            child_max_chars=self._settings.child_max_chars,
            child_overlap_chars=self._settings.child_overlap_chars,
            fallback_heading_path=document.title,
        )
        if not parents:
            raise AppError(ErrorCode.PARSE_EMPTY_CONTENT, "切块结果为空")
        self._log_stage(source_id, "chunk", stage)
        self._check_deadline(deadline)
        stage = time.perf_counter()
        child_ids, embedding_texts = self._persist_chunks(
            source_id, parents
        )
        self._log_stage(source_id, "persist", stage)
        self._check_deadline(deadline)
        stage = time.perf_counter()
        embeddings = self._embedder.embed(
            embedding_texts, budget_s=deadline - time.monotonic()
        )
        self._log_stage(source_id, "embed", stage)
        self._check_deadline(deadline)
        stage = time.perf_counter()
        for child_id, text in zip(
            child_ids, embedding_texts, strict=True
        ):
            self._fts_index.upsert(child_id, text)
        self._vector_index.upsert(
            child_ids,
            embeddings,
            [{"source_id": source_id} for _ in child_ids],
        )
        self._log_stage(source_id, "index", stage)
        self._check_deadline(deadline)
        self._finish(source_id, document)
        return IngestResult(
            source_id=source_id,
            status=SourceStatus.COMPLETED.value,
            triggered=True,
            child_count=len(child_ids),
        )

    def _persist_chunks(
        self, source_id: int, parents: Sequence[DomainParent]
    ) -> tuple[list[int], list[str]]:
        with self._session_factory() as session:
            session.execute(
                delete(ChildChunk).where(ChildChunk.source_id == source_id)
            )
            session.execute(
                delete(ParentChunk).where(ParentChunk.source_id == source_id)
            )
            session.flush()
            child_ids, embedding_texts = self._insert_chunks(
                session, source_id, parents
            )
            session.commit()
        return child_ids, embedding_texts

    @staticmethod
    def _insert_chunks(
        session: Session,
        source_id: int,
        parents: Sequence[DomainParent],
        *,
        start_parent_id: int | None = None,
        start_child_id: int | None = None,
    ) -> tuple[list[int], list[str]]:
        child_ids: list[int] = []
        embedding_texts: list[str] = []
        next_parent_id = start_parent_id
        next_child_id = start_child_id
        for parent in parents:
            parent_row = ParentChunk(
                source_id=source_id,
                ordinal=parent.ordinal,
                heading_path=parent.heading_path,
                content=parent.content,
            )
            if next_parent_id is not None:
                parent_row.id = next_parent_id
                next_parent_id += 1
            session.add(parent_row)
            session.flush()
            for child in parent.children:
                child_row = ChildChunk(
                    source_id=source_id,
                    parent_id=parent_row.id,
                    ordinal=child.ordinal,
                    text=child.text,
                    embedding_text=child.embedding_text,
                    content_type=child.content_type,
                    heading_path=child.heading_path,
                )
                if next_child_id is not None:
                    child_row.id = next_child_id
                    next_child_id += 1
                session.add(child_row)
                session.flush()
                child_ids.append(child_row.id)
                embedding_texts.append(child.embedding_text)
        return child_ids, embedding_texts

    def _finish(self, source_id: int, document: ParsedDocument) -> None:
        with self._session_factory() as session:
            self._apply_source_success(
                session,
                source_id,
                document,
                hashlib.sha256(
                    document.markdown.encode("utf-8")
                ).hexdigest(),
            )
            session.commit()

    def _apply_source_success(
        self,
        session: Session,
        source_id: int,
        document: ParsedDocument,
        content_hash: str,
    ) -> None:
        source = session.get(KnowledgeSource, source_id)
        if source is None:
            raise AppError(ErrorCode.NOT_FOUND, f"来源不存在: {source_id}")
        now = self._clock()
        source.status = SourceStatus.COMPLETED.value
        source.title = document.title
        source.content_hash = content_hash
        source.chunker_version = self._chunker_version
        source.embedding_model = self._settings.embedding_model
        source.error_code = None
        source.error_message = None
        source.processed_at = now
        source.last_checked_at = now
        source.updated_at = now

    def _delete_child_indexes(self, child_ids: Sequence[int]) -> None:
        if not child_ids:
            return
        for stage, action in (
            ("vector", lambda: self._vector_index.delete_many(child_ids)),
            ("fts", lambda: self._fts_index.delete_many(child_ids)),
        ):
            try:
                action()
            except Exception:
                logger.warning(
                    "清理子块索引失败",
                    extra={"stage": stage, "count": len(child_ids)},
                )

    def _compensate(self, source_id: int) -> None:
        child_ids = self._child_ids(source_id)
        steps = (
            ("vector", lambda: self._vector_index.delete_by_source(source_id)),
            ("fts", lambda: self._fts_index.delete_many(child_ids)),
        )
        for stage, action in steps:
            try:
                action()
            except Exception:
                logger.warning(
                    "补偿清理派生索引失败",
                    extra={"source_id": source_id, "stage": stage},
                )
        try:
            with self._session_factory() as session:
                session.execute(
                    delete(ChildChunk).where(ChildChunk.source_id == source_id)
                )
                session.execute(
                    delete(ParentChunk).where(ParentChunk.source_id == source_id)
                )
                session.commit()
        except Exception:
            logger.warning(
                "补偿清理关系数据失败",
                extra={"source_id": source_id, "stage": "db"},
            )

    def _child_count(self, source_id: int) -> int:
        with self._session_factory() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(ChildChunk)
                    .where(ChildChunk.source_id == source_id)
                )
                or 0
            )

    def _child_ids(self, source_id: int) -> list[int]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(ChildChunk.id).where(ChildChunk.source_id == source_id)
                ).all()
            )

    def _max_parent_child_ids(self) -> tuple[int, int]:
        with self._session_factory() as session:
            parent_max = session.scalar(
                select(func.max(ParentChunk.id))
            )
            child_max = session.scalar(
                select(func.max(ChildChunk.id))
            )
            return int(parent_max or 0), int(child_max or 0)

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if time.monotonic() > deadline:
            raise AppError(ErrorCode.INGEST_TIMEOUT, "入库超过时间预算")

    @staticmethod
    def _log_stage(source_id: int, stage: str, started: float) -> None:
        logger.info(
            "ingestion_stage",
            extra={
                "source_id": source_id,
                "stage": stage,
                "duration_ms": round(
                    (time.perf_counter() - started) * 1000, 2
                ),
            },
        )


def _error_code(error: Exception) -> ErrorCode:
    if isinstance(error, AppError):
        return error.code
    if isinstance(error, (FetchError, ParseError, EmbeddingError)):
        return error.code
    if isinstance(error, TimeoutError):
        return ErrorCode.INGEST_TIMEOUT
    return ErrorCode.INTERNAL_ERROR


def _markdown_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None
