"""SQLite 权威存储层（见《项目介绍.md》5.1、5.3）。"""

from __future__ import annotations

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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DEFAULT_BUSY_TIMEOUT_MS = 5000
SCHEMA_VERSION = "1"


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
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[str | None] = mapped_column(Text)


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
    """按 ORM 模型创建全部关系表，并写入 schema_version。"""

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        if session.get(IndexMeta, "schema_version") is None:
            session.add(IndexMeta(key="schema_version", value=SCHEMA_VERSION))
            session.commit()
