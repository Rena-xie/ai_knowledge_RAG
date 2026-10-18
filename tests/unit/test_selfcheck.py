"""启动自检行为（见《项目开发说明_v1.md》10.6）。"""

from pathlib import Path

import pytest

from app.core.selfcheck import (
    SelfCheckError,
    check_fts5_available,
    ensure_ascii_path,
    ensure_writable_dir,
)


def test_fts5_is_available(tmp_path: Path) -> None:
    check_fts5_available(tmp_path / "app.db")


def test_writable_dir_accepts_new_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "chroma"

    ensure_writable_dir(target)

    assert target.is_dir()


def test_writable_dir_rejects_path_under_file(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(SelfCheckError):
        ensure_writable_dir(blocker / "chroma")


def test_ascii_path_is_accepted(tmp_path: Path) -> None:
    ensure_ascii_path(tmp_path / "chroma")


def test_non_ascii_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SelfCheckError):
        ensure_ascii_path(tmp_path / "知识库" / "chroma")
