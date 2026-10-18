"""评测执行器与题集加载（见《项目开发说明_v1.md》11.4）。"""

from pathlib import Path

import pytest

from app.services.evaluation import (
    EvalQuestion,
    EvaluationRunner,
    load_questions,
)
from app.services.qa import AnswerCitation, AnswerResult
from app.services.retrieval import RetrievalResult


class FakeRetrieval:
    def __init__(self, mapping: dict[str, tuple[int, ...]]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    def search(self, question: str) -> RetrievalResult:
        self.calls.append(question)
        fused = self._mapping.get(question, ())
        return RetrievalResult(
            evidences=(),
            dense_ids=fused,
            bm25_ids=fused,
            fused_ids=fused,
            max_similarity=0.7,
        )


class FakeQA:
    def __init__(self, mapping: dict[str, AnswerResult]) -> None:
        self._mapping = mapping
        self.retrievals: list[RetrievalResult | None] = []

    def answer(
        self, question: str, *, retrieval: RetrievalResult | None = None
    ) -> AnswerResult:
        self.retrievals.append(retrieval)
        return self._mapping[question]


def answer(
    text: str, *, kind: str = "answered", validated: bool = True
) -> AnswerResult:
    citations = (
        (AnswerCitation(1, 2, "标题", "https://example.com/a", "H"),)
        if kind == "answered"
        else ()
    )
    return AnswerResult(
        text=text,
        answer_type=kind,
        citations=citations,
        citations_validated=validated,
    )


def build_runner() -> EvaluationRunner:
    retrieval = FakeRetrieval(
        {"命中题": (7, 8, 9), "未命中题": (1, 2, 3), "无答案题": ()}
    )
    qa = FakeQA(
        {
            "命中题": answer("答案 [S1:C2]"),
            "未命中题": answer("答案 [S1:C2]", validated=False),
            "无答案题": answer("知识库缺少相关证据。", kind="insufficient_evidence"),
        }
    )
    return EvaluationRunner(retrieval_service=retrieval, qa_service=qa)


def test_reports_metrics() -> None:
    questions = [
        EvalQuestion("q1", "专有名词", "命中题", (9,)),
        EvalQuestion("q2", "语义改写", "未命中题", (5,)),
        EvalQuestion("q3", "无答案", "无答案题"),
    ]

    report = build_runner().run(questions)

    assert report.total == 3
    assert report.recall_at_5 == pytest.approx(0.5)
    assert report.citation_validity == pytest.approx(0.5)
    assert report.insufficient_accuracy == pytest.approx(1.0)
    assert [result.recall_hit for result in report.results] == [True, False, None]
    assert report.precision_at_5 == pytest.approx(0.1666)
    assert report.results[0].precision_at_5 == pytest.approx(0.3333)
    assert report.results[1].precision_at_5 == pytest.approx(0.0)
    assert report.results[2].precision_at_5 is None


def test_report_serializes_to_dict() -> None:
    report = build_runner().run(
        [EvalQuestion("q1", "专有名词", "命中题", (7,))]
    )

    payload = report.as_dict()

    assert payload["total"] == 1
    assert payload["recall_at_5"] == 1.0
    assert payload["results"][0]["question_id"] == "q1"
    assert payload["precision_at_5"] == pytest.approx(0.3333)


def test_metrics_are_none_without_denominator() -> None:
    report = build_runner().run([EvalQuestion("q1", "无答案", "无答案题")])

    assert report.recall_at_5 is None
    assert report.citation_validity is None
    assert report.precision_at_5 is None
    assert report.insufficient_accuracy == 1.0


def test_runner_reuses_one_retrieval_per_question() -> None:
    """每题只检索一次，并把结果复用给问答（见 11.4）。"""

    retrieval = FakeRetrieval({"命中题": (7, 8, 9)})
    qa = FakeQA({"命中题": answer("答案 [S1:C2]")})
    runner = EvaluationRunner(retrieval_service=retrieval, qa_service=qa)

    report = runner.run([EvalQuestion("q1", "专有名词", "命中题", (7,))])

    assert retrieval.calls == ["命中题"]
    assert qa.retrievals[0] is not None
    assert qa.retrievals[0].fused_ids == (7, 8, 9)
    assert report.results[0].max_similarity == pytest.approx(0.7)


def test_load_questions_reads_json(tmp_path: Path) -> None:
    path = tmp_path / "questions.json"
    path.write_text(
        """
        {
          "version": 1,
          "questions": [
            {"id": "q1", "category": "专有名词", "question": "问题",
             "relevant_child_ids": [3, 4]},
            {"id": "q2", "category": "无答案", "question": "无关问题"}
          ]
        }
        """,
        encoding="utf-8",
    )

    questions = load_questions(path)

    assert [item.id for item in questions] == ["q1", "q2"]
    assert questions[0].relevant_child_ids == (3, 4)
    assert questions[1].relevant_child_ids == ()


def test_load_questions_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "questions.json"
    path.write_text(
        """
        {
          "version": 1,
          "questions": [
            {"id": "q1", "category": "专有名词", "question": "问题一"},
            {"id": "q1", "category": "语义改写", "question": "问题二"}
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_questions(path)
