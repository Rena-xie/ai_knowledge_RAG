"""SQLite 权威存储层（见《项目开发说明_v1.md》5.1、5.3）。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Engine,
    Float,
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
SCHEMA_VERSION = "5"
DEFAULT_COLLECTION_ID = "default"
DEFAULT_COLLECTION_NAME = "默认知识库"


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
    fingerprint_version: Mapped[str | None] = mapped_column(Text)
    language_code: Mapped[str | None] = mapped_column(Text)
    identity_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending"
    )
    identity_verified_at: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[str | None] = mapped_column(Text)


class SourceAlias(Base):
    """来源的提交、重定向和历史 URL 别名。"""

    __tablename__ = "source_aliases"
    __table_args__ = (
        Index("ix_source_aliases_source_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    alias_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class KnowledgeCollection(Base):
    """知识集合。"""

    __tablename__ = "knowledge_collections"
    __table_args__ = (
        Index(
            "uq_knowledge_collections_default",
            "is_default",
            unique=True,
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class KnowledgeCollectionSource(Base):
    """知识集合与来源的多对多关联。"""

    __tablename__ = "knowledge_collection_sources"
    __table_args__ = (
        Index("ix_collection_sources_source_id", "source_id"),
    )

    collection_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[str] = mapped_column(Text, nullable=False)


class Tag(Base):
    """全局来源标签。"""

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class SourceTag(Base):
    """来源与标签的多对多关联。"""

    __tablename__ = "source_tags"
    __table_args__ = (
        Index("ix_source_tags_tag_id", "tag_id"),
    )

    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[str] = mapped_column(Text, nullable=False)


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
    force_refresh: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="SET NULL")
    )
    final_url: Mapped[str | None] = mapped_column(Text)
    duplicate_of_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="SET NULL")
    )
    changed: Mapped[bool | None] = mapped_column(Boolean)
    title: Mapped[str | None] = mapped_column(Text)
    skip_reason: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)

    job: Mapped[IngestionJob] = relationship(back_populates="items")


class JobIdempotencyKey(Base):
    """按 24 小时时间桶登记 Job 幂等键。"""

    __tablename__ = "job_idempotency_keys"
    __table_args__ = (
        Index("ix_job_idempotency_keys_job_id", "job_id"),
    )

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket_start: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), nullable=False
    )


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


class ApiCostEvent(Base):
    """外部 API 单次 HTTP 尝试的用量与成本事件。"""

    __tablename__ = "api_cost_events"
    __table_args__ = (
        Index("ix_api_cost_events_created_at", "created_at"),
        Index("ix_api_cost_events_job_id", "job_id"),
        Index("ix_api_cost_events_request_id", "request_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_units: Mapped[int] = mapped_column(nullable=False)
    usage_source: Mapped[str] = mapped_column(Text, nullable=False)
    price_per_million: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    cost: Mapped[float | None] = mapped_column(Float)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    request_count: Mapped[int] = mapped_column(nullable=False, default=1)
    attempt: Mapped[int] = mapped_column(nullable=False, default=1)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column()


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
    """把旧数据库升级到 v4；未知版本保持原值，避免误降级。"""

    table_names = set(inspect(engine).get_table_names())
    if "knowledge_sources" in table_names:
        _ensure_last_checked_at(engine)
        _ensure_source_identity_columns(engine)
        _ensure_content_hash_index(engine)
    if "ingestion_job_items" in table_names:
        _ensure_force_refresh(engine)
        _ensure_job_item_identity_columns(engine)
    if "index_meta" not in table_names:
        return

    with Session(engine) as session:
        meta = session.get(IndexMeta, "schema_version")
        if meta is None:
            session.add(IndexMeta(key="schema_version", value=SCHEMA_VERSION))
            session.commit()
            version = SCHEMA_VERSION
        else:
            version = meta.value

    if version == "1":
        with Session(engine) as session:
            meta = session.get(IndexMeta, "schema_version")
            if meta is not None:
                meta.value = "2"
                session.commit()
        version = "2"
    if version == "2":
        _ensure_force_refresh(engine)
        with Session(engine) as session:
            meta = session.get(IndexMeta, "schema_version")
            if meta is not None:
                meta.value = "3"
                session.commit()
        version = "3"
    if version == "3":
        with Session(engine) as session:
            meta = session.get(IndexMeta, "schema_version")
            if meta is not None:
                meta.value = "4"
                session.commit()
        version = "4"
    if version == "4":
        with Session(engine) as session:
            meta = session.get(IndexMeta, "schema_version")
            if meta is not None:
                meta.value = SCHEMA_VERSION
                session.commit()
        version = SCHEMA_VERSION
    if version != SCHEMA_VERSION:
        logger.warning(
            "未知 schema_version，保持原值",
            extra={"schema_version": version},
        )
        return
    _ensure_default_collection(engine)
    _backfill_legacy_source_aliases(engine)
    _backfill_idempotency_keys(engine)


def _ensure_last_checked_at(engine: Engine) -> None:
    columns = {
        column["name"] for column in inspect(engine).get_columns("knowledge_sources")
    }
    if "last_checked_at" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE knowledge_sources ADD COLUMN last_checked_at TEXT")
            )


def _ensure_force_refresh(engine: Engine) -> None:
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("ingestion_job_items")
    }
    if "force_refresh" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE ingestion_job_items "
                    "ADD COLUMN force_refresh BOOLEAN NOT NULL DEFAULT 0"
                )
            )


def _ensure_source_identity_columns(engine: Engine) -> None:
    columns = {
        column["name"] for column in inspect(engine).get_columns("knowledge_sources")
    }
    additions = {
        "fingerprint_version": "TEXT",
        "language_code": "TEXT",
        "identity_status": "TEXT NOT NULL DEFAULT 'legacy_unverified'",
        "identity_verified_at": "TEXT",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE knowledge_sources "
                        f"ADD COLUMN {name} {definition}"
                    )
                )


def _ensure_job_item_identity_columns(engine: Engine) -> None:
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("ingestion_job_items")
    }
    additions = {
        "final_url": "TEXT",
        "duplicate_of_source_id": (
            "INTEGER REFERENCES knowledge_sources(id) ON DELETE SET NULL"
        ),
        "changed": "BOOLEAN",
        "skip_reason": "TEXT",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE ingestion_job_items "
                        f"ADD COLUMN {name} {definition}"
                    )
                )


def _ensure_content_hash_index(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_knowledge_sources_content_hash "
                "ON knowledge_sources(content_hash)"
            )
        )


def _ensure_default_collection(engine: Engine) -> None:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT OR IGNORE INTO knowledge_collections "
                "(id, name, description, is_default, created_at, updated_at) "
                "VALUES (:id, :name, :description, 1, :now, :now)"
            ),
            {
                "id": DEFAULT_COLLECTION_ID,
                "name": DEFAULT_COLLECTION_NAME,
                "description": "未指定集合的来源",
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT OR IGNORE INTO knowledge_collection_sources "
                "(collection_id, source_id, added_at) "
                "SELECT :collection_id, id, :now FROM knowledge_sources"
            ),
            {"collection_id": DEFAULT_COLLECTION_ID, "now": now},
        )


def _backfill_legacy_source_aliases(engine: Engine) -> None:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT OR IGNORE INTO source_aliases "
                "(source_id, url, normalized_url, alias_type, created_at) "
                "SELECT id, canonical_url, canonical_url, "
                "'legacy_provisional', :now FROM knowledge_sources"
            ),
            {"now": now},
        )


def _backfill_idempotency_keys(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT OR IGNORE INTO job_idempotency_keys "
                "(key, bucket_start, job_id) "
                "SELECT idempotency_key, substr(created_at, 1, 10), id "
                "FROM ingestion_jobs WHERE idempotency_key IS NOT NULL"
            )
        )
