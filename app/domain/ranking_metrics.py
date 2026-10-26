"""排序质量指标。"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

RECALL_K = 5
METRIC_K = 10


@dataclass(frozen=True)
class RankingMetrics:
    """一次排序结果的二值相关性指标。"""

    recall_at_5: float
    precision_at_5: float
    hit_at_5: bool
    mrr_at_10: float
    ndcg_at_10: float


def calculate_ranking_metrics(
    relevant_ids: Sequence[int],
    ranked_ids: Sequence[int],
) -> RankingMetrics | None:
    """按去重后的相关 ID 计算标准排序指标。"""

    relevant = set(relevant_ids)
    if not relevant:
        return None
    ranked = tuple(dict.fromkeys(ranked_ids))
    top_five = ranked[:RECALL_K]
    relevant_hits = sum(child_id in relevant for child_id in top_five)
    return RankingMetrics(
        recall_at_5=round(relevant_hits / len(relevant), 4),
        precision_at_5=round(relevant_hits / RECALL_K, 4),
        hit_at_5=relevant_hits > 0,
        mrr_at_10=round(_reciprocal_rank(ranked, relevant), 4),
        ndcg_at_10=round(_ndcg(ranked, relevant), 4),
    )


def _reciprocal_rank(
    ranked_ids: Sequence[int],
    relevant: set[int],
) -> float:
    for rank, child_id in enumerate(ranked_ids[:METRIC_K], start=1):
        if child_id in relevant:
            return 1.0 / rank
    return 0.0


def _ndcg(ranked_ids: Sequence[int], relevant: set[int]) -> float:
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, child_id in enumerate(ranked_ids[:METRIC_K], start=1)
        if child_id in relevant
    )
    ideal_hits = min(len(relevant), METRIC_K)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0
