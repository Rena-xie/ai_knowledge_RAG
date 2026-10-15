"""启动自检：目录可写、SQLite 可用且启用 FTS5（见《项目介绍.md》10.6）。"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from app.core.settings import Settings


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


def ensure_ascii_path(path: Path) -> None:
    """确保路径解析后为纯 ASCII（见《项目介绍.md》10.6 第 5 条）。

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

    ensure_writable_dir(settings.sqlite_path.parent)
    check_fts5_available(settings.sqlite_path)
    ensure_ascii_path(settings.chroma_path)
    ensure_writable_dir(settings.chroma_path)
