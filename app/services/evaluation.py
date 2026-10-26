"""检索评测。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.ranking_metrics import RankingMetrics, calculate_ranking_metrics
from app.services.retrieval import RetrievalService

LANGUAGE_LABELS = frozenset({"zh", "en", "mixed"})


@dataclass(frozen=True)
class EvalQuestion:
    """一道评测题；无标注表示检索应返回空证据。"""

    id: str
    category: str
    question: str
    relevant_child_ids: tuple[int, ...] = ()
    relevant_parent_ids: tuple[int, ...] = ()
    language: str = "zh"


@dataclass(frozen=True)
class QuestionResult:
    """单题结果。"""

    question_id: str
    category: str
    language: str
    retrieved_child_ids: tuple[int, ...]
    retrieved_parent_ids: tuple[int, ...]
    fused_ids: tuple[int, ...]
    recall_at_5: float | None
    hit_at_5: bool | None
    empty_evidence: bool
    max_similarity: float = 0.0
    precision_at_5: float | None = None
    mrr_at_10: float | None = None
    ndcg_at_10: float | None = None
    rerank_applied: bool = False
    child_recall_at_5: float | None = None
    child_precision_at_5: float | None = None
    child_hit_at_5: bool | None = None
    child_mrr_at_10: float | None = None
    child_ndcg_at_10: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "category": self.category,
            "language": self.language,
            "retrieved_child_ids": list(self.retrieved_child_ids),
            "retrieved_parent_ids": list(self.retrieved_parent_ids),
            "fused_ids": list(self.fused_ids),
            "recall_at_5": self.recall_at_5,
            "hit_at_5": self.hit_at_5,
            "empty_evidence": self.empty_evidence,
            "max_similarity": self.max_similarity,
            "precision_at_5": self.precision_at_5,
            "mrr_at_10": self.mrr_at_10,
            "ndcg_at_10": self.ndcg_at_10,
            "rerank_applied": self.rerank_applied,
            "child_recall_at_5": self.child_recall_at_5,
            "child_precision_at_5": self.child_precision_at_5,
            "child_hit_at_5": self.child_hit_at_5,
            "child_mrr_at_10": self.child_mrr_at_10,
            "child_ndcg_at_10": self.child_ndcg_at_10,
        }


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
            "results": [question_result.as_dict() for question_result in self.results],
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
        question_results = [self._evaluate_question(question) for question in questions]
        return self._summarize(questions, question_results)

    def _evaluate_question(self, question: EvalQuestion) -> QuestionResult:
        retrieval = self._retrieval_service.search(question.question)
        retrieved_children = tuple(
            evidence.child_id for evidence in retrieval.evidences[:10]
        )
        retrieved_parents = tuple(
            evidence.parent_id for evidence in retrieval.evidences[:10]
        )
        parent_metrics = calculate_ranking_metrics(
            question.relevant_parent_ids, retrieved_parents
        )
        child_metrics = calculate_ranking_metrics(
            question.relevant_child_ids, retrieved_children
        )
        return QuestionResult(
            question_id=question.id,
            category=question.category,
            language=question.language,
            retrieved_child_ids=retrieved_children,
            retrieved_parent_ids=retrieved_parents,
            fused_ids=tuple(retrieval.fused_ids[:10]),
            **_metrics_fields(parent_metrics),
            empty_evidence=not retrieval.evidences,
            max_similarity=retrieval.max_similarity,
            rerank_applied=retrieval.rerank_applied,
            **_child_metric_fields(child_metrics),
        )

    @staticmethod
    def _summarize(
        questions: Sequence[EvalQuestion], results: list[QuestionResult]
    ) -> EvalReport:
        labeled = [
            result
            for result, item in zip(results, questions, strict=True)
            if item.relevant_child_ids
        ]
        unlabeled = [
            result
            for result, item in zip(results, questions, strict=True)
            if not item.relevant_child_ids
        ]
        recall_scores = [
            result.recall_at_5 for result in labeled if result.recall_at_5 is not None
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
        mrrs = [result.mrr_at_10 for result in labeled if result.mrr_at_10 is not None]
        ndcgs = [
            result.ndcg_at_10 for result in labeled if result.ndcg_at_10 is not None
        ]
        return EvalReport(
            total=len(results),
            recall_at_5=_average(recall_scores),
            hit_rate_at_5=_average(hit_scores),
            empty_evidence_accuracy=_ratio(len(empty_evidence), len(unlabeled)),
            precision_at_5=(
                round(sum(precisions) / len(precisions), 4) if precisions else None
            ),
            mrr_at_10=(round(sum(mrrs) / len(mrrs), 4) if mrrs else None),
            ndcg_at_10=(round(sum(ndcgs) / len(ndcgs), 4) if ndcgs else None),
            category_metrics=_category_metrics(questions, results),
            results=tuple(results),
        )


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


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
        labeled = [result for question, result in paired if question.relevant_child_ids]
        unlabeled = [
            result for question, result in paired if not question.relevant_child_ids
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
                sum(1 for result in unlabeled if result.empty_evidence),
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
    map_file = payload.get("child_parent_map_file")
    if map_file is None:
        raise ValueError("题库缺少 child_parent_map_file")
    child_parent_map = load_child_parent_map(path.parent / str(map_file))
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
        relevant_child_ids = tuple(
            int(value) for value in raw.get("relevant_child_ids", [])
        )
        questions.append(
            EvalQuestion(
                id=question_id,
                category=str(raw["category"]),
                question=str(raw["question"]),
                relevant_child_ids=relevant_child_ids,
                relevant_parent_ids=_relevant_parent_ids(
                    relevant_child_ids,
                    child_parent_map,
                    question_id,
                ),
                language=language,
            )
        )
    if not questions:
        raise ValueError("题库为空")
    return questions


def load_child_parent_map(path: Path) -> dict[int, int]:
    """加载完整 Child 到 Parent 映射，并拒绝重复键。"""

    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_object_keys,
    )
    raw_mapping = payload.get("child_parent_ids")
    if not isinstance(raw_mapping, dict):
        raise ValueError(f"Child 到 Parent 映射格式无效: {path}")
    mapping = {
        int(child_id): int(parent_id) for child_id, parent_id in raw_mapping.items()
    }
    if any(child_id <= 0 or parent_id <= 0 for child_id, parent_id in mapping.items()):
        raise ValueError(f"Child 到 Parent 映射包含非正数: {path}")
    return mapping


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError(f"JSON 存在重复键: {key}")
        parsed[key] = value
    return parsed


def _relevant_parent_ids(
    child_ids: Sequence[int],
    child_parent_map: dict[int, int],
    question_id: str,
) -> tuple[int, ...]:
    missing = [child_id for child_id in child_ids if child_id not in child_parent_map]
    if missing:
        raise ValueError(f"题目 {question_id} 缺少 Child 到 Parent 映射: {missing}")
    return tuple(dict.fromkeys(child_parent_map[child_id] for child_id in child_ids))


def _metrics_fields(metrics: RankingMetrics | None) -> dict[str, Any]:
    if metrics is None:
        return {
            "recall_at_5": None,
            "hit_at_5": None,
            "precision_at_5": None,
            "mrr_at_10": None,
            "ndcg_at_10": None,
        }
    return {
        "recall_at_5": metrics.recall_at_5,
        "hit_at_5": metrics.hit_at_5,
        "precision_at_5": metrics.precision_at_5,
        "mrr_at_10": metrics.mrr_at_10,
        "ndcg_at_10": metrics.ndcg_at_10,
    }


def _child_metric_fields(
    metrics: RankingMetrics | None,
) -> dict[str, Any]:
    if metrics is None:
        return {
            "child_recall_at_5": None,
            "child_precision_at_5": None,
            "child_hit_at_5": None,
            "child_mrr_at_10": None,
            "child_ndcg_at_10": None,
        }
    return {
        "child_recall_at_5": metrics.recall_at_5,
        "child_precision_at_5": metrics.precision_at_5,
        "child_hit_at_5": metrics.hit_at_5,
        "child_mrr_at_10": metrics.mrr_at_10,
        "child_ndcg_at_10": metrics.ndcg_at_10,
    }
