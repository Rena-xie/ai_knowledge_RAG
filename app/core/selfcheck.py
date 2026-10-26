"""启动自检：目录可写、SQLite 可用且启用 FTS5（见《项目开发说明_v1.md》10.6）。"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from app.core.settings import Settings

REQUIRED_TEXT_SETTINGS = (
    "embedding_api_base",
    "embedding_api_key",
    "embedding_model",
    "rerank_model",
    "fetch_user_agent",
    "api_provider",
    "api_cost_currency",
)
POSITIVE_SETTINGS = (
    "dense_top_k",
    "bm25_top_k",
    "rrf_k",
    "final_top_k",
    "rerank_top_n",
    "rerank_timeout_s",
    "fetch_timeout_s",
    "fetch_max_bytes",
    "embed_timeout_s",
    "ingest_timeout_s",
    "ingest_lock_timeout_s",
    "ingestion_max_urls",
    "ingestion_queue_max",
    "ingestion_job_timeout_s",
    "parent_max_chars",
    "child_target_chars",
    "child_max_chars",
    "context_budget_chars",
    "content_preview_parent_limit",
    "max_request_bytes",
    "app_port",
)
NONNEGATIVE_SETTINGS = (
    "fetch_max_retries",
    "fetch_max_redirects",
    "embed_max_retries",
    "rerank_max_retries",
    "route_head_reserve",
    "child_overlap_chars",
    "polite_delay_s",
    "robots_cache_ttl_s",
)


class SelfCheckError(RuntimeError):
    """启动自检失败。"""


def ensure_writable_dir(path: Path) -> None:
    """确保目录存在且可写。"""

    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".selfcheck-", delete=True):
            pass
    except OSError as exc:
        raise SelfCheckError(f"目录不可写: {path}: {exc}") from exc


def validate_runtime_settings(settings: Settings) -> None:
    """拒绝启动时已知无效的模型配置和资源边界。"""

    for field_name in REQUIRED_TEXT_SETTINGS:
        if not str(getattr(settings, field_name)).strip():
            raise SelfCheckError(f"配置 {field_name} 不能为空")
    for field_name in POSITIVE_SETTINGS:
        if getattr(settings, field_name) <= 0:
            raise SelfCheckError(f"配置 {field_name} 必须大于 0")
    for field_name in NONNEGATIVE_SETTINGS:
        if getattr(settings, field_name) < 0:
            raise SelfCheckError(f"配置 {field_name} 不能小于 0")
    if not 0 <= settings.min_dense_similarity <= 1:
        raise SelfCheckError("配置 min_dense_similarity 必须在 0 到 1 之间")
    for field_name in (
        "embedding_input_price_per_million",
        "rerank_input_price_per_million",
    ):
        value = getattr(settings, field_name)
        if value is not None and value < 0:
            raise SelfCheckError(f"配置 {field_name} 不能小于 0")
    budget = settings.api_cost_budget_24h
    if budget is not None and budget <= 0:
        raise SelfCheckError("配置 api_cost_budget_24h 必须大于 0")
    if budget is not None and settings.embedding_input_price_per_million is None:
        raise SelfCheckError("启用预算时必须配置 Embedding 单价")
    if (
        budget is not None
        and settings.rerank_enabled
        and settings.rerank_input_price_per_million is None
    ):
        raise SelfCheckError("启用预算时必须配置 rerank 单价")


def ensure_ascii_path(path: Path) -> None:
    """确保路径解析后为纯 ASCII（见《项目开发说明_v1.md》10.6 第 5 条）。

    chromadb 在含非 ASCII 字符的路径下可以写入，但重启后无法读取 HNSW
    索引（报 `Error loading hnsw index`），因此在启动阶段直接拒绝。
    """

    resolved = Path(path).resolve()
    try:
        str(resolved).encode("ascii")
    except UnicodeEncodeError as exc:
        raise SelfCheckError(
            "Chroma 持久化目录必须解析为纯 ASCII 路径，"
            f"当前路径包含非 ASCII 字符: {resolved}"
        ) from exc


def check_fts5_available(db_path: Path) -> None:
    """确保 SQLite 可用且编译启用了 FTS5。"""

    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        raise SelfCheckError(f"SQLite 不可用: {db_path}: {exc}") from exc
    try:
        options = {row[0] for row in connection.execute("PRAGMA compile_options")}
    finally:
        connection.close()
    if "ENABLE_FTS5" not in options:
        raise SelfCheckError(
            "当前 SQLite 未启用 FTS5（PRAGMA compile_options 缺少 ENABLE_FTS5）"
        )


def run_startup_checks(settings: Settings) -> None:
    """执行全部启动自检，任一失败即抛出 SelfCheckError。"""

    validate_runtime_settings(settings)
    ensure_writable_dir(settings.sqlite_path.parent)
    check_fts5_available(settings.sqlite_path)
    ensure_ascii_path(settings.chroma_path)
    ensure_writable_dir(settings.chroma_path)
