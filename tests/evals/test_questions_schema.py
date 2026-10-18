"""题库格式与覆盖面（见《项目开发说明_v1.md》11.4）。"""

from pathlib import Path

from app.services.evaluation import load_questions

QUESTIONS_PATH = Path(__file__).parent / "questions.json"
CATEGORIES = {"专有名词", "语义改写", "代码查询", "跨章节", "无答案"}


def test_question_set_covers_all_categories() -> None:
    questions = load_questions(QUESTIONS_PATH)

    assert len(questions) >= 20
    assert {item.category for item in questions} >= CATEGORIES


def test_only_no_answer_questions_are_unlabeled() -> None:
    questions = load_questions(QUESTIONS_PATH)

    for item in questions:
        if item.category == "无答案":
            assert item.relevant_child_ids == (), item.id
        else:
            assert item.relevant_child_ids, item.id
