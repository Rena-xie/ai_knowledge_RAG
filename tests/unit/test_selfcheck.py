"""启动自检行为（见《项目介绍.md》10.6）。"""

from pathlib import Path

import pytest

from app.core.selfcheck import SelfCheckError, check_fts5_available, ensure_writable_dir


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
