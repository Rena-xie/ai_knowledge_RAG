"""题库、映射与评测基线契约。"""

import json
import re
from pathlib import Path

import pytest

from app.services.evaluation import load_child_parent_map, load_questions

QUESTIONS_PATH = Path(__file__).parent / "questions.json"
LANGUAGE_SLICE_PATH = Path(__file__).parent / "questions_language_slices.json"
CHILD_PARENT_MAP_PATH = Path(__file__).parent / "child_parent_map.json"
RAW_RESULTS_PATH = (
    Path(__file__).parent / "results" / "2026-10-26-v2-evidence-148q.json"
)
BASELINE_PATH = (
    Path(__file__).parent
    / "results"
    / "2026-10-26-v2-parent-evidence-148q-baseline.json"
)
CLEAN_BASELINE_PATH = (
    Path(__file__).parent
    / "results"
    / "2026-10-26-v2-parent-clean-r2-148q.json"
)
CATEGORIES = {"专有名词", "语义改写", "代码查询", "跨章节", "无答案"}
LANGUAGES = {"zh", "en", "mixed"}


def test_question_set_covers_all_categories() -> None:
    questions = load_questions(QUESTIONS_PATH)

    assert len(questions) == 148
    assert {item.category for item in questions} >= CATEGORIES
    assert {item.language for item in questions} == LANGUAGES


def test_question_set_keeps_84_independent_groups() -> None:
    questions = load_questions(QUESTIONS_PATH)
    groups = {re.sub(r"[a-z]+$", "", item.id) for item in questions}

    assert len(groups) == 84
    assert sum(item.language == "zh" for item in questions) == 128
    assert sum(item.language == "en" for item in questions) == 10
    assert sum(item.language == "mixed" for item in questions) == 10


def test_language_slice_file_matches_main_question_labels() -> None:
    main_questions = load_questions(QUESTIONS_PATH)
    slices = load_questions(LANGUAGE_SLICE_PATH)
    expected = {
        item.id: item for item in main_questions if item.language in {"en", "mixed"}
    }

    assert len(slices) == 20
    assert {item.id for item in slices} == set(expected)
    for item in slices:
        assert item.question == expected[item.id].question
        assert item.relevant_child_ids == expected[item.id].relevant_child_ids
        assert item.relevant_parent_ids == expected[item.id].relevant_parent_ids


def test_child_parent_map_covers_labels_and_retrieved_children() -> None:
    questions = load_questions(QUESTIONS_PATH)
    results = json.loads(RAW_RESULTS_PATH.read_text(encoding="utf-8"))
    child_parent_map = load_child_parent_map(CHILD_PARENT_MAP_PATH)
    needed_ids = {
        child_id for question in questions for child_id in question.relevant_child_ids
    }
    needed_ids.update(
        child_id for result in results["results"] for child_id in result["retrieved"]
    )

    assert needed_ids <= set(child_parent_map)


def test_grouped_baseline_contract() -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    assert baseline["questions_version"] == 8
    assert baseline["totals"]["groups"] == 84
    assert baseline["totals"]["variants"] == 148
    assert baseline["totals"]["recall_at_5"] == pytest.approx(0.83)
    assert baseline["totals"]["precision_at_5"] == pytest.approx(0.1975)
    assert baseline["totals"]["hit_rate_at_5"] == pytest.approx(0.8734)
    assert baseline["totals"]["mrr_at_10"] == pytest.approx(0.8161)
    assert baseline["totals"]["ndcg_at_10"] == pytest.approx(0.7986)
    assert baseline["totals"]["child_recall_at_5"] == pytest.approx(0.6323)
    assert baseline["totals"]["empty_evidence_accuracy"] == 1.0
    assert baseline["metric_unit"] == "parent"
    assert baseline["metric_source"] == "final_parent_evidences"
    assert baseline["child_metrics_usage"] == "diagnostic_only"
    assert baseline["baseline_status"] == "provisional"
    assert baseline["child_parent_map_file"] == "child_parent_map.json"
    assert set(baseline["language_metrics"]) == LANGUAGES
    assert baseline["language_metrics"]["en"]["variants"] == 10
    assert baseline["language_metrics"]["mixed"]["variants"] == 10
    assert baseline["missing_question_ids"] == []
    assert baseline["source_commit"] == "b88ceb4"


def test_clean_parent_baseline_contract() -> None:
    baseline = json.loads(CLEAN_BASELINE_PATH.read_text(encoding="utf-8"))

    assert baseline["totals"]["groups"] == 84
    assert baseline["totals"]["variants"] == 148
    assert baseline["totals"]["recall_at_5"] == pytest.approx(0.8368)
    assert baseline["totals"]["precision_at_5"] == pytest.approx(0.2005)
    assert baseline["totals"]["hit_rate_at_5"] == pytest.approx(0.8734)
    assert baseline["totals"]["mrr_at_10"] == pytest.approx(0.8009)
    assert baseline["totals"]["ndcg_at_10"] == pytest.approx(0.7956)
    assert baseline["totals"]["empty_evidence_accuracy"] == 1.0
    assert baseline["metric_unit"] == "parent"
    assert baseline["metric_source"] == "final_parent_evidences"
    assert baseline["baseline_status"] == "accepted"
    assert baseline["missing_question_ids"] == []
    assert baseline["source_commit"] == "765712fc12f78d847e5fd6f438930b33634dc810"


def test_only_no_answer_questions_are_unlabeled() -> None:
    questions = load_questions(QUESTIONS_PATH)

    for item in questions:
        if item.category == "无答案":
            assert item.relevant_child_ids == (), item.id
        else:
            assert item.relevant_child_ids, item.id
