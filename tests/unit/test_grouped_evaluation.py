"""独立问题与语言维度的评测聚合。"""

import json
from pathlib import Path

import pytest

from scripts.eval_grouped_metrics import group_results, merge_result_payloads


def _question(
    question_id: str,
    category: str,
    *,
    language: str | None = None,
    labels: tuple[int, ...] = (1,),
) -> dict[str, object]:
    question: dict[str, object] = {
        "id": question_id,
        "category": category,
        "question": question_id,
        "relevant_child_ids": list(labels),
    }
    if language is not None:
        question["language"] = language
    return question


def _result(
    question_id: str,
    *,
    recall: float | None,
    precision: float | None,
    hit: bool | None,
    mrr: float | None,
    ndcg: float | None,
    empty: bool = False,
) -> dict[str, object]:
    return {
        "question_id": question_id,
        "recall_at_5": recall,
        "precision_at_5": precision,
        "hit_at_5": hit,
        "mrr_at_10": mrr,
        "ndcg_at_10": ndcg,
        "retrieved": [] if empty else [1],
        "empty_evidence": empty,
    }


def test_groups_by_independent_question_and_language() -> None:
    questions = {
        "version": 7,
        "default_language": "zh",
        "questions": [
            _question("q01", "专有名词"),
            _question("q01a", "专有名词", language="en"),
            _question("q01b", "专有名词", language="mixed"),
            _question("q02", "语义改写"),
            _question("q03", "无答案", labels=()),
        ],
    }
    results = {
        "results": [
            _result(
                "q01",
                recall=1.0,
                precision=0.2,
                hit=True,
                mrr=1.0,
                ndcg=1.0,
            ),
            _result(
                "q01a",
                recall=0.5,
                precision=0.2,
                hit=True,
                mrr=0.5,
                ndcg=0.5,
            ),
            _result(
                "q01b",
                recall=0.0,
                precision=0.0,
                hit=False,
                mrr=0.0,
                ndcg=0.0,
            ),
            _result(
                "q02",
                recall=0.5,
                precision=0.2,
                hit=False,
                mrr=0.0,
                ndcg=0.0,
            ),
            _result(
                "q03",
                recall=None,
                precision=None,
                hit=None,
                mrr=None,
                ndcg=None,
                empty=True,
            ),
        ]
    }

    payload = group_results(questions, results)

    assert payload["totals"]["groups"] == 3
    assert payload["totals"]["variants"] == 5
    assert payload["totals"]["recall_at_5"] == pytest.approx(0.5)
    assert payload["totals"]["precision_at_5"] == pytest.approx(0.1667)
    assert payload["totals"]["hit_rate_at_5"] == pytest.approx(0.3333)
    assert payload["totals"]["empty_evidence_accuracy"] == 1.0
    assert payload["language_metrics"]["zh"]["variants"] == 3
    assert payload["language_metrics"]["zh"]["recall_at_5"] == pytest.approx(
        0.75
    )
    assert payload["language_metrics"]["en"]["groups"] == 1
    assert payload["language_metrics"]["mixed"]["recall_at_5"] == 0.0


def test_merge_results_rejects_duplicate_question(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(
        json.dumps({"rerank_enabled": True, "results": [{"question_id": "q1"}]}),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({"rerank_enabled": True, "results": [{"question_id": "q1"}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="重复题目"):
        merge_result_payloads([first, second])
