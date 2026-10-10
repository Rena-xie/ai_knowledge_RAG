"""检索与问答评测（见《项目介绍.md》11.4）。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.qa import QAService
from app.services.retrieval import RetrievalService

RECALL_K = 5


@dataclass(frozen=True)
class EvalQuestion:
    """一道评测题；无标注（relevant_child_ids 为空）表示应回答「证据不足」。"""

    id: str
    category: str
    question: str
    relevant_child_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class QuestionResult:
    """单题结果。"""

    question_id: str
    category: str
    retrieved: tuple[int, ...]
    recall_hit: bool | None
    answer_type: str
    citations_validated: bool
    citation_count: int


@dataclass(frozen=True)
class EvalReport:
    """评测汇总；分母为 0 时对应指标为 None。"""

    total: int
    recall_at_5: float | None
    citation_validity: float | None
    insufficient_accuracy: float | None
    results: tuple[QuestionResult, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "recall_at_5": self.recall_at_5,
            "citation_validity": self.citation_validity,
            "insufficient_accuracy": self.insufficient_accuracy,
            "results": [
                {
                    "question_id": result.question_id,
                    "category": result.category,
                    "retrieved": list(result.retrieved),
                    "recall_hit": result.recall_hit,
                    "answer_type": result.answer_type,
                    "citations_validated": result.citations_validated,
                    "citation_count": result.citation_count,
                }
                for result in self.results
            ],
        }


class EvaluationRunner:
    """按题库执行检索与问答，输出 Recall@5、引用有效率与无答案识别率。"""

    def __init__(
        self,
        *,
        retrieval_service: RetrievalService,
        qa_service: QAService,
    ) -> None:
        self._retrieval_service = retrieval_service
        self._qa_service = qa_service

    def run(self, questions: Sequence[EvalQuestion]) -> EvalReport:
        results: list[QuestionResult] = []
        for item in questions:
            retrieval = self._retrieval_service.search(item.question)
            answer = self._qa_service.answer(item.question)
            top_k = tuple(retrieval.fused_ids[:RECALL_K])
            recall_hit: bool | None = None
            if item.relevant_child_ids:
                recall_hit = bool(set(top_k) & set(item.relevant_child_ids))
            results.append(
                QuestionResult(
                    question_id=item.id,
                    category=item.category,
                    retrieved=top_k,
                    recall_hit=recall_hit,
                    answer_type=answer.answer_type,
                    citations_validated=answer.citations_validated,
                    citation_count=len(answer.citations),
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
        answered = [
            result for result in results if result.answer_type == "answered"
        ]
        unlabeled = [
            result for result, item in zip(results, questions, strict=True)
            if not item.relevant_child_ids
        ]
        recall_hits = [result for result in labeled if result.recall_hit]
        validated = [result for result in answered if result.citations_validated]
        insufficient = [
            result
            for result in unlabeled
            if result.answer_type == "insufficient_evidence"
        ]
        return EvalReport(
            total=len(results),
            recall_at_5=_ratio(len(recall_hits), len(labeled)),
            citation_validity=_ratio(len(validated), len(answered)),
            insufficient_accuracy=_ratio(len(insufficient), len(unlabeled)),
            results=tuple(results),
        )


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def load_questions(path: Path) -> list[EvalQuestion]:
    """从 JSON 题库加载评测题，校验 id 唯一。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    questions: list[EvalQuestion] = []
    seen: set[str] = set()
    for raw in payload.get("questions", []):
        question_id = str(raw["id"])
        if question_id in seen:
            raise ValueError(f"题库存在重复 id: {question_id}")
        seen.add(question_id)
        questions.append(
            EvalQuestion(
                id=question_id,
                category=str(raw["category"]),
                question=str(raw["question"]),
                relevant_child_ids=tuple(
                    int(value) for value in raw.get("relevant_child_ids", [])
                ),
            )
        )
    if not questions:
        raise ValueError("题库为空")
    return questions
