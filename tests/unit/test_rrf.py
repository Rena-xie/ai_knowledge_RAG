"""RRF 融合（见《项目介绍.md》7.2）。"""

import pytest

from app.domain.rrf import rrf_fuse


def test_single_path_uses_reciprocal_rank() -> None:
    hits = rrf_fuse([11, 22], [], k=60)

    assert [(hit.child_id, hit.best_rank) for hit in hits] == [(11, 1), (22, 2)]
    assert hits[0].score == pytest.approx(1 / 61)
    assert hits[1].score == pytest.approx(1 / 62)


def test_child_in_both_paths_accumulates_score() -> None:
    hits = rrf_fuse([7], [7], k=60)

    assert len(hits) == 1
    assert hits[0].score == pytest.approx(1 / 61 + 1 / 61)


def test_two_paths_are_combined() -> None:
    hits = {hit.child_id: hit for hit in rrf_fuse([1, 2], [2, 3], k=60)}

    assert hits[2].score > hits[1].score
    assert hits[2].best_rank == 1


def test_tie_break_uses_best_rank_then_smaller_id() -> None:
    hits = rrf_fuse([5, 3], [3, 5], k=60)

    assert [hit.child_id for hit in hits] == [3, 5]


def test_duplicate_ids_within_one_path_count_once() -> None:
    hits = rrf_fuse([4, 4, 9], [], k=60)

    assert [hit.child_id for hit in hits] == [4, 9]
    assert hits[0].score == pytest.approx(1 / 61)


def test_top_k_limits_results() -> None:
    hits = rrf_fuse([1, 2, 3], [], k=60, top_k=2)

    assert [hit.child_id for hit in hits] == [1, 2]


def test_empty_inputs() -> None:
    assert rrf_fuse([], [], k=60) == []
    assert rrf_fuse([], [1], k=60)[0].child_id == 1


def test_head_reserve_pulls_route_champions_to_front() -> None:
    # 3 与 4 在两路都有命中（共识分更高），但各路的第 1 名是 1 与 2
    hits = rrf_fuse([1, 3, 4], [2, 4, 3], k=60, head_reserve=1)

    assert [hit.child_id for hit in hits][:2] == [1, 2]
    # 其余仍按 RRF 分数排序：3 与 4 得分相同，best_rank 更小的 3 在前
    assert [hit.child_id for hit in hits][2:] == [3, 4]


def test_head_reserve_keeps_every_hit_once() -> None:
    hits = rrf_fuse([1, 2], [1, 2], k=60, head_reserve=1)

    assert [hit.child_id for hit in hits] == [1, 2]


def test_head_reserve_respects_top_k() -> None:
    hits = rrf_fuse([9, 8, 7], [6, 5], k=60, top_k=3, head_reserve=1)

    # 保底 [9, 6] 之后按分数排序：8 与 5 同分同 best_rank，按 id 升序取 5
    assert [hit.child_id for hit in hits] == [9, 6, 5]


def test_head_reserve_zero_keeps_pure_score_order() -> None:
    hits = rrf_fuse([1, 3, 4], [2, 4, 3], k=60, head_reserve=0)

    assert [hit.child_id for hit in hits] == [3, 4, 1, 2]
