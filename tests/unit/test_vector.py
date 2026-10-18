"""Chroma 向量索引（见《项目开发说明_v1.md》7.1、ADR-007）。"""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.infra.vector import VectorIndex


@pytest.fixture
def index(tmp_path: Path) -> Iterator[VectorIndex]:
    yield VectorIndex(tmp_path / "chroma")


def test_collection_uses_cosine_space(index: VectorIndex) -> None:
    assert index.space == "cosine"


def test_query_returns_nearest_child(index: VectorIndex) -> None:
    index.upsert(
        child_ids=[1, 2],
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        metadatas=[{"source_id": 10}, {"source_id": 10}],
    )

    hits = index.query([0.9, 0.1, 0.0], top_k=1)

    assert [hit.child_id for hit in hits] == [1]
    assert hits[0].similarity == pytest.approx(0.9938, abs=0.01)


def test_upsert_overwrites_same_child(index: VectorIndex) -> None:
    index.upsert([1], [[1.0, 0.0, 0.0]], [{"source_id": 10}])
    index.upsert([1], [[0.0, 1.0, 0.0]], [{"source_id": 10}])

    assert index.count() == 1
    assert [hit.child_id for hit in index.query([0.0, 1.0, 0.0], top_k=1)] == [1]


def test_delete_by_source_removes_only_that_source(index: VectorIndex) -> None:
    index.upsert(
        child_ids=[1, 2],
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
        metadatas=[{"source_id": 10}, {"source_id": 20}],
    )

    index.delete_by_source(10)

    assert index.count() == 1
    assert [hit.child_id for hit in index.query([1.0, 0.0, 0.0], top_k=1)] == [2]


def test_close_releases_files(tmp_path: Path) -> None:
    path = tmp_path / "chroma"
    index = VectorIndex(path)
    index.upsert([1], [[1.0, 0.0, 0.0]], [{"source_id": 1}])

    index.close()

    shutil.rmtree(path)  # Windows 下若文件仍被占用会抛 PermissionError
    assert not path.exists()
