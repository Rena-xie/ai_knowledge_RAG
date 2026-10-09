"""入库流水线（见《项目介绍.md》4.4、5.3、5.4）。"""

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

    def ingest(self, url: str) -> IngestResult:
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
            raise AppError(ErrorCode.INGEST_BUSY, "已有入库任务在执行")
        try:
            return self._run_pipeline(source_id, url)
        finally:
            self._lock.release()

    def _run_pipeline(self, source_id: int, url: str) -> IngestResult:
        deadline = time.monotonic() + self._settings.ingest_timeout_s
        try:
            self._source_service.mark_status(source_id, SourceStatus.PROCESSING)
            fetched = self._fetcher.fetch(url)
            self._check_deadline(deadline)
            document = self._parser(fetched.html, url=fetched.url)
            parents = chunk_markdown(
                document.markdown,
                parent_max_chars=self._settings.parent_max_chars,
                child_target_chars=self._settings.child_target_chars,
                child_max_chars=self._settings.child_max_chars,
                child_overlap_chars=self._settings.child_overlap_chars,
            )
            if not parents:
                raise AppError(ErrorCode.PARSE_EMPTY_CONTENT, "切块结果为空")
            self._check_deadline(deadline)
            child_ids, embedding_texts = self._persist_chunks(source_id, parents)
            embeddings = self._embedder.embed(embedding_texts)
            self._check_deadline(deadline)
            for child_id, text in zip(child_ids, embedding_texts, strict=True):
                self._fts_index.upsert(child_id, text)
            self._vector_index.upsert(
                child_ids,
                embeddings,
                [{"source_id": source_id} for _ in child_ids],
            )
            self._check_deadline(deadline)
            self._finish(source_id, document)
            return IngestResult(
                source_id=source_id,
                status=SourceStatus.COMPLETED.value,
                triggered=True,
                child_count=len(child_ids),
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

    def _persist_chunks(
        self, source_id: int, parents: Sequence[DomainParent]
    ) -> tuple[list[int], list[str]]:
        child_ids: list[int] = []
        embedding_texts: list[str] = []
        with self._session_factory() as session:
            session.execute(
                delete(ChildChunk).where(ChildChunk.source_id == source_id)
            )
            session.execute(
                delete(ParentChunk).where(ParentChunk.source_id == source_id)
            )
            for parent in parents:
                parent_row = ParentChunk(
                    source_id=source_id,
                    ordinal=parent.ordinal,
                    heading_path=parent.heading_path,
                    content=parent.content,
                )
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
                    session.add(child_row)
                    session.flush()
                    child_ids.append(child_row.id)
                    embedding_texts.append(child.embedding_text)
            session.commit()
        return child_ids, embedding_texts

    def _finish(self, source_id: int, document: ParsedDocument) -> None:
        with self._session_factory() as session:
            source = session.get(KnowledgeSource, source_id)
            if source is None:
                raise AppError(ErrorCode.NOT_FOUND, f"来源不存在: {source_id}")
            now = self._clock()
            source.status = SourceStatus.COMPLETED.value
            source.title = document.title
            source.content_hash = hashlib.sha256(
                document.markdown.encode("utf-8")
            ).hexdigest()
            source.chunker_version = self._chunker_version
            source.embedding_model = self._settings.embedding_model
            source.error_code = None
            source.error_message = None
            source.processed_at = now
            source.updated_at = now
            session.commit()

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

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if time.monotonic() > deadline:
            raise AppError(ErrorCode.INGEST_TIMEOUT, "入库超过时间预算")


def _error_code(error: Exception) -> ErrorCode:
    if isinstance(error, AppError):
        return error.code
    if isinstance(error, (FetchError, ParseError, EmbeddingError)):
        return error.code
    if isinstance(error, TimeoutError):
        return ErrorCode.INGEST_TIMEOUT
    return ErrorCode.INTERNAL_ERROR
