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
    retrieved_child_ids: tuple[int, ...],
) -> dict[str, object]:
    return {
        "question_id": question_id,
        "retrieved": list(retrieved_child_ids),
        "empty_evidence": not retrieved_child_ids,
    }


def test_groups_by_independent_question_and_language() -> None:
    questions = {
        "version": 8,
        "default_language": "zh",
        "child_parent_map_file": "child_parent_map.json",
        "questions": [
            _question("q01", "专有名词", labels=(1, 2)),
            _question("q01a", "专有名词", language="en", labels=(1, 2)),
            _question(
                "q01b",
                "专有名词",
                language="mixed",
                labels=(1, 2),
            ),
            _question("q02", "语义改写", labels=(3,)),
            _question("q03", "无答案", labels=()),
        ],
    }
    results = {
        "results": [
            _result("q01", retrieved_child_ids=(1, 2)),
            _result("q01a", retrieved_child_ids=(1,)),
            _result("q01b", retrieved_child_ids=()),
            _result("q02", retrieved_child_ids=(3,)),
            _result("q03", retrieved_child_ids=()),
        ]
    }

    payload = group_results(
        questions,
        results,
        {1: 10, 2: 10, 3: 30},
    )

    assert payload["totals"]["groups"] == 3
    assert payload["totals"]["variants"] == 5
    assert payload["totals"]["recall_at_5"] == pytest.approx(0.8334)
    assert payload["totals"]["precision_at_5"] == pytest.approx(0.1667)
    assert payload["totals"]["hit_rate_at_5"] == pytest.approx(0.8334)
    assert payload["totals"]["child_recall_at_5"] == pytest.approx(0.75)
    assert payload["totals"]["empty_evidence_accuracy"] == 1.0
    assert payload["language_metrics"]["zh"]["variants"] == 3
    assert payload["language_metrics"]["zh"]["recall_at_5"] == 1.0
    assert payload["language_metrics"]["en"]["groups"] == 1
    assert payload["language_metrics"]["en"]["recall_at_5"] == 1.0
    assert payload["language_metrics"]["mixed"]["recall_at_5"] == 0.0
    assert payload["metric_unit"] == "parent"


def test_group_results_rejects_missing_parent_mapping() -> None:
    questions = {
        "version": 8,
        "child_parent_map_file": "child_parent_map.json",
        "questions": [_question("q01", "专有名词")],
    }
    results = {"results": [_result("q01", retrieved_child_ids=(1, 99))]}

    with pytest.raises(ValueError, match="缺少 Parent 映射"):
        group_results(questions, results, {1: 10})


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
