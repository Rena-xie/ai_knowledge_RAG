"""SQLite 权威存储层（见《项目开发说明_v1.md》5.1、5.3）。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Engine,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

logger = logging.getLogger(__name__)

DEFAULT_BUSY_TIMEOUT_MS = 5000
SCHEMA_VERSION = "2"


class Base(DeclarativeBase):
    """ORM 基类。"""


class KnowledgeSource(Base):
    """知识来源表（权威数据）。"""

    __tablename__ = "knowledge_sources"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    chunker_version: Mapped[str | None] = mapped_column(Text)
    embedding_model: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[str | None] = mapped_column(Text)


class IngestionJob(Base):
    """异步入库任务（v2）。"""

    __tablename__ = "ingestion_jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    total_items: Mapped[int] = mapped_column(nullable=False)
    succeeded_items: Mapped[int] = mapped_column(nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(nullable=False, default=0)
    skipped_items: Mapped[int] = mapped_column(nullable=False, default=0)
    client_run_id: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list[IngestionJobItem]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class IngestionJobItem(Base):
    """入库任务中的单个 URL（v2）。"""

    __tablename__ = "ingestion_job_items"
    __table_args__ = (
        Index("ix_ingestion_job_items_job", "job_id", "id"),
        Index("ix_ingestion_job_items_status", "status"),
        Index("ix_ingestion_job_items_canonical", "canonical_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="SET NULL")
    )
    title: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)

    job: Mapped[IngestionJob] = relationship(back_populates="items")


class ParentChunk(Base):
    """父级切块表。"""

    __tablename__ = "parent_chunks"
    __table_args__ = (
        UniqueConstraint("source_id", "ordinal", name="uq_parent_source_ordinal"),
        Index("ix_parent_chunks_source_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(nullable=False)
    heading_path: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)


class ChildChunk(Base):
    """子级切块表，child_id 为三处索引的关联标识。"""

    __tablename__ = "child_chunks"
    __table_args__ = (
        UniqueConstraint("parent_id", "ordinal", name="uq_child_parent_ordinal"),
        Index("ix_child_chunks_source_id", "source_id"),
        Index("ix_child_chunks_parent_id", "parent_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[int] = mapped_column(
        ForeignKey("parent_chunks.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    heading_path: Mapped[str] = mapped_column(Text, nullable=False)


class IndexMeta(Base):
    """索引元数据（schema/模型/切块版本）。"""

    __tablename__ = "index_meta"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


def create_db_engine(
    sqlite_path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> Engine:
    """创建启用 WAL、busy_timeout 与外键约束的 SQLite 引擎。"""

    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{sqlite_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": busy_timeout_ms / 1000},
    )

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建会话工厂；每个请求使用独立会话。"""

    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """按 ORM 模型创建全部关系表，并执行幂等 schema 迁移。"""

    Base.metadata.create_all(engine)
    _migrate_schema(engine)


def _migrate_schema(engine: Engine) -> None:
    """把 v1 数据库升级到 v2；未知版本保持原值，避免误降级。"""

    table_names = set(inspect(engine).get_table_names())
    if "knowledge_sources" in table_names:
        _ensure_last_checked_at(engine)
    if "index_meta" not in table_names:
        return

    with Session(engine) as session:
        meta = session.get(IndexMeta, "schema_version")
        if meta is None:
            session.add(IndexMeta(key="schema_version", value=SCHEMA_VERSION))
            session.commit()
            return
        version = meta.value

    if version == SCHEMA_VERSION:
        return
    if version != "1":
        logger.warning(
            "未知 schema_version，保持原值",
            extra={"schema_version": version},
        )
        return

    with Session(engine) as session:
        meta = session.get(IndexMeta, "schema_version")
        if meta is not None:
            meta.value = SCHEMA_VERSION
            session.commit()


def _ensure_last_checked_at(engine: Engine) -> None:
    columns = {
        column["name"] for column in inspect(engine).get_columns("knowledge_sources")
    }
    if "last_checked_at" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE knowledge_sources ADD COLUMN last_checked_at TEXT")
            )
