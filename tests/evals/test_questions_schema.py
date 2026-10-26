"""题库格式与覆盖面（见《项目开发说明_v1.md》11.4）。"""

import json
import re
from pathlib import Path

import pytest

from app.services.evaluation import load_questions

QUESTIONS_PATH = Path(__file__).parent / "questions.json"
LANGUAGE_SLICE_PATH = Path(__file__).parent / "questions_language_slices.json"
BASELINE_PATH = (
    Path(__file__).parent
    / "results"
    / "2026-10-26-v2-core-148q-baseline.json"
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
    groups = {
        re.sub(r"[a-z]+$", "", item.id) for item in questions
    }

    assert len(groups) == 84
    assert sum(item.language == "zh" for item in questions) == 128
    assert sum(item.language == "en" for item in questions) == 10
    assert sum(item.language == "mixed" for item in questions) == 10


def test_language_slice_file_matches_main_question_labels() -> None:
    main_questions = load_questions(QUESTIONS_PATH)
    slices = load_questions(LANGUAGE_SLICE_PATH)
    expected = {
        item.id: item
        for item in main_questions
        if item.language in {"en", "mixed"}
    }

    assert len(slices) == 20
    assert {item.id for item in slices} == set(expected)
    for item in slices:
        assert item.question == expected[item.id].question
        assert item.relevant_child_ids == expected[item.id].relevant_child_ids


def test_grouped_baseline_contract() -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    assert baseline["questions_version"] == 7
    assert baseline["totals"]["groups"] == 84
    assert baseline["totals"]["variants"] == 148
    assert baseline["totals"]["precision_at_5"] == pytest.approx(0.2147)
    assert baseline["totals"]["empty_evidence_accuracy"] == 0.0
    assert set(baseline["language_metrics"]) == LANGUAGES
    assert baseline["language_metrics"]["en"]["variants"] == 10
    assert baseline["language_metrics"]["mixed"]["variants"] == 10
    assert baseline["missing_question_ids"] == []
    assert baseline["source_commit"] == "00d7d78"


def test_only_no_answer_questions_are_unlabeled() -> None:
    questions = load_questions(QUESTIONS_PATH)

    for item in questions:
        if item.category == "无答案":
            assert item.relevant_child_ids == (), item.id
        else:
            assert item.relevant_child_ids, item.id
