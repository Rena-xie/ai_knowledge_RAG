"""检索评测。"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.retrieval import RetrievalService

RECALL_K = 5
LANGUAGE_LABELS = frozenset({"zh", "en", "mixed"})


@dataclass(frozen=True)
class EvalQuestion:
    """一道评测题；无标注表示检索应返回空证据。"""

    id: str
    category: str
    question: str
    relevant_child_ids: tuple[int, ...] = ()
    language: str = "zh"


@dataclass(frozen=True)
class QuestionResult:
    """单题结果。"""

    question_id: str
    category: str
    language: str
    retrieved: tuple[int, ...]
    fused_ids: tuple[int, ...]
    recall_at_5: float | None
    hit_at_5: bool | None
    empty_evidence: bool
    max_similarity: float = 0.0
    precision_at_5: float | None = None
    mrr_at_10: float | None = None
    ndcg_at_10: float | None = None
    rerank_applied: bool = False


@dataclass(frozen=True)
class EvalReport:
    """评测汇总；分母为 0 时对应指标为 None。"""

    total: int
    recall_at_5: float | None
    hit_rate_at_5: float | None
    empty_evidence_accuracy: float | None
    precision_at_5: float | None
    mrr_at_10: float | None
    ndcg_at_10: float | None
    category_metrics: dict[str, dict[str, object]]
    results: tuple[QuestionResult, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "recall_at_5": self.recall_at_5,
            "hit_rate_at_5": self.hit_rate_at_5,
            "empty_evidence_accuracy": self.empty_evidence_accuracy,
            "precision_at_5": self.precision_at_5,
            "mrr_at_10": self.mrr_at_10,
            "ndcg_at_10": self.ndcg_at_10,
            "category_metrics": self.category_metrics,
            "results": [
                {
                    "question_id": result.question_id,
                    "category": result.category,
                    "language": result.language,
                    "retrieved": list(result.retrieved),
                    "fused_ids": list(result.fused_ids),
                    "recall_at_5": result.recall_at_5,
                    "hit_at_5": result.hit_at_5,
                    "empty_evidence": result.empty_evidence,
                    "max_similarity": result.max_similarity,
                    "precision_at_5": result.precision_at_5,
                    "mrr_at_10": result.mrr_at_10,
                    "ndcg_at_10": result.ndcg_at_10,
                    "rerank_applied": result.rerank_applied,
                }
                for result in self.results
            ],
        }


class EvaluationRunner:
    """按题库执行检索，输出召回、排序和空证据指标。"""

    def __init__(
        self,
        *,
        retrieval_service: RetrievalService,
    ) -> None:
        self._retrieval_service = retrieval_service

    def run(self, questions: Sequence[EvalQuestion]) -> EvalReport:
        results: list[QuestionResult] = []
        for item in questions:
            retrieval = self._retrieval_service.search(item.question)
            retrieved = tuple(
                evidence.child_id for evidence in retrieval.evidences[:10]
            )
            fused_ids = tuple(retrieval.fused_ids[:10])
            top_k = retrieved[:RECALL_K]
            recall: float | None = None
            hit: bool | None = None
            precision: float | None = None
            mrr: float | None = None
            ndcg: float | None = None
            if item.relevant_child_ids:
                relevant = set(item.relevant_child_ids)
                hits = len(set(top_k) & relevant)
                # 标准 Recall@5：命中的相关块数 / 该题全部相关块数。
                recall = round(hits / len(relevant), 4)
                hit = hits > 0
                # 标准 Precision@5 的分母固定为 5，即使返回结果不足 5 条。
                precision = round(hits / RECALL_K, 4)
                mrr = round(_mrr(item.relevant_child_ids, retrieved), 4)
                ndcg = round(_ndcg(item.relevant_child_ids, retrieved), 4)
            results.append(
                QuestionResult(
                    question_id=item.id,
                    category=item.category,
                    language=item.language,
                    retrieved=retrieved,
                    fused_ids=fused_ids,
                    recall_at_5=recall,
                    hit_at_5=hit,
                    empty_evidence=not retrieval.evidences,
                    max_similarity=retrieval.max_similarity,
                    precision_at_5=precision,
                    mrr_at_10=mrr,
                    ndcg_at_10=ndcg,
                    rerank_applied=retrieval.rerank_applied,
                )
            )
        return self._summarize(questions, results)

    @staticmethod
    def _summarize(
        questions: Sequence[EvalQuestion], results: list[QuestionResult]
    ) -> EvalReport:
        labeled = [
            result for result, item in zip(results, questions, strict=True)
            if item.relevant_child_ids
        ]
        unlabeled = [
            result for result, item in zip(results, questions, strict=True)
            if not item.relevant_child_ids
        ]
        recall_scores = [
            result.recall_at_5
            for result in labeled
            if result.recall_at_5 is not None
        ]
        hit_scores = [
            1.0 if result.hit_at_5 else 0.0
            for result in labeled
            if result.hit_at_5 is not None
        ]
        empty_evidence = [result for result in unlabeled if result.empty_evidence]
        precisions = [
            result.precision_at_5
            for result in labeled
            if result.precision_at_5 is not None
        ]
        mrrs = [
            result.mrr_at_10
            for result in labeled
            if result.mrr_at_10 is not None
        ]
        ndcgs = [
            result.ndcg_at_10
            for result in labeled
            if result.ndcg_at_10 is not None
        ]
        return EvalReport(
            total=len(results),
            recall_at_5=_average(recall_scores),
            hit_rate_at_5=_average(hit_scores),
            empty_evidence_accuracy=_ratio(len(empty_evidence), len(unlabeled)),
            precision_at_5=(
                round(sum(precisions) / len(precisions), 4) if precisions else None
            ),
            mrr_at_10=(
                round(sum(mrrs) / len(mrrs), 4) if mrrs else None
            ),
            ndcg_at_10=(
                round(sum(ndcgs) / len(ndcgs), 4) if ndcgs else None
            ),
            category_metrics=_category_metrics(questions, results),
            results=tuple(results),
        )


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _mrr(relevant: Sequence[int], retrieved: Sequence[int]) -> float:
    relevant_set = set(relevant)
    for rank, child_id in enumerate(retrieved, start=1):
        if child_id in relevant_set:
            return 1.0 / rank
    return 0.0


def _ndcg(relevant: Sequence[int], retrieved: Sequence[int]) -> float:
    relevant_set = set(relevant)
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, child_id in enumerate(retrieved[:10], start=1)
        if child_id in relevant_set
    )
    ideal_hits = min(len(relevant_set), 10)
    idcg = sum(
        1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1)
    )
    return dcg / idcg if idcg else 0.0


def _category_metrics(
    questions: Sequence[EvalQuestion], results: Sequence[QuestionResult]
) -> dict[str, dict[str, object]]:
    categories = sorted({item.category for item in questions})
    payload: dict[str, dict[str, object]] = {}
    for category in categories:
        paired = [
            (question, result)
            for question, result in zip(questions, results, strict=True)
            if question.category == category
        ]
        labeled = [
            result for question, result in paired if question.relevant_child_ids
        ]
        unlabeled = [
            result
            for question, result in paired
            if not question.relevant_child_ids
        ]
        recalls = [result.recall_at_5 for result in labeled]
        hits = sum(1 for result in labeled if result.hit_at_5)
        payload[category] = {
            "total": len(paired),
            "labeled": len(labeled),
            "recall_at_5": _average(recalls),
            "hit_rate_at_5": _ratio(hits, len(labeled)),
            "mrr_at_10": _average([result.mrr_at_10 for result in labeled]),
            "ndcg_at_10": _average([result.ndcg_at_10 for result in labeled]),
            "empty_evidence_accuracy": _ratio(
                sum(
                    1
                    for result in unlabeled
                    if result.empty_evidence
                ),
                len(unlabeled),
            ),
        }
    return payload


def _average(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 4)


def load_questions(path: Path) -> list[EvalQuestion]:
    """从 JSON 题库加载评测题，校验 id 唯一。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    default_language = str(payload.get("default_language", "zh"))
    if default_language not in LANGUAGE_LABELS:
        raise ValueError(f"未知默认语言: {default_language}")
    questions: list[EvalQuestion] = []
    seen: set[str] = set()
    for raw in payload.get("questions", []):
        question_id = str(raw["id"])
        if question_id in seen:
            raise ValueError(f"题库存在重复 id: {question_id}")
        seen.add(question_id)
        language = str(raw.get("language", default_language))
        if language not in LANGUAGE_LABELS:
            raise ValueError(f"题目语言不合法: {question_id}={language}")
        questions.append(
            EvalQuestion(
                id=question_id,
                category=str(raw["category"]),
                question=str(raw["question"]),
                relevant_child_ids=tuple(
                    int(value) for value in raw.get("relevant_child_ids", [])
                ),
                language=language,
            )
        )
    if not questions:
        raise ValueError("题库为空")
    return questions
