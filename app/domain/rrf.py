"""RRF 倒数排名融合（见《项目开发说明_v1.md》7.2）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_RRF_K = 60


@dataclass(frozen=True)
class FusedHit:
    """融合后的 Child 命中项。"""

    child_id: int
    score: float
    best_rank: int


def rrf_fuse(
    dense_ids: Sequence[int],
    bm25_ids: Sequence[int],
    *,
    k: int = DEFAULT_RRF_K,
    top_k: int | None = None,
    head_reserve: int = 0,
) -> list[FusedHit]:
    """按 RRF 融合两路召回结果。

    同一 child_id 在两路出现时分数累加；并列时先比较单路最佳排名，
    再按 child_id 升序，保证结果稳定。``head_reserve`` 让每一路召回的前
    N 名直接进入结果头部（见《项目开发说明_v1.md》7.2 与 ADR-012）。
    """

    if k <= 0:
        raise ValueError("k 必须为正数")

    scores: dict[int, float] = {}
    best_ranks: dict[int, int] = {}
    for ids in (dense_ids, bm25_ids):
        seen: set[int] = set()
        for rank, child_id in enumerate(ids, start=1):
            if child_id in seen:
                continue
            seen.add(child_id)
            scores[child_id] = scores.get(child_id, 0.0) + 1.0 / (k + rank)
            current = best_ranks.get(child_id)
            best_ranks[child_id] = rank if current is None else min(current, rank)

    hits = [
        FusedHit(child_id=child_id, score=score, best_rank=best_ranks[child_id])
        for child_id, score in scores.items()
    ]
    hits.sort(key=lambda hit: (-hit.score, hit.best_rank, hit.child_id))
    if head_reserve > 0:
        by_id = {hit.child_id: hit for hit in hits}
        reserved: list[int] = []
        for ids in (dense_ids, bm25_ids):
            for child_id in ids[:head_reserve]:
                if child_id in by_id and child_id not in reserved:
                    reserved.append(child_id)
        head = [by_id[child_id] for child_id in reserved]
        tail = [hit for hit in hits if hit.child_id not in set(reserved)]
        hits = head + tail
    if top_k is not None:
        return hits[:top_k]
    return hits
