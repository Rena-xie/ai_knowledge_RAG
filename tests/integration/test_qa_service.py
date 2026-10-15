"""问答服务：证据拼装、引用校验与证据不足（见《项目介绍.md》7.4）。"""

from pathlib import Path

import pytest

from app.core.errors import AppError, ErrorCode
from app.infra.llm import LLMError, LLMResult, load_prompt
from app.services.qa import (
    INSUFFICIENT_EVIDENCE_TEXT,
    QAService,
)
from app.services.retrieval import Evidence, RetrievalResult


class FakeRetrieval:
    def __init__(self, result: RetrievalResult) -> None:
        self._result = result
        self.questions: list[str] = []

    def search(self, question: str) -> RetrievalResult:
        self.questions.append(question)
        return self._result


class FakeLLM:
    def __init__(
        self, *, text: str = "答案 [S1:C2]", error: LLMError | None = None
    ) -> None:
        self._text = text
        self._error = error
        self.calls = 0
        self.last_user_prompt = ""
        self.last_system_prompt = ""

    def complete(self, *, system_prompt: str, user_prompt: str) -> LLMResult:
        self.calls += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        if self._error is not None:
            raise self._error
        return LLMResult(text=self._text, prompt_tokens=10, completion_tokens=5)


def make_evidence(child_id: int = 2) -> Evidence:
    return Evidence(
        source_id=1,
        child_id=child_id,
        parent_id=9,
        title="示例标题",
        url="https://example.com/a",
        heading_path="H",
        text="证据正文",
        score=1.0,
    )


def make_result(*evidences: Evidence) -> RetrievalResult:
    return RetrievalResult(
        evidences=tuple(evidences),
        dense_ids=(2,),
        bm25_ids=(2,),
        fused_ids=(2,),
    )


@pytest.fixture
def prompt_dir(tmp_path: Path) -> Path:
    path = tmp_path / "system_prompt_v1.md"
    path.write_text(
        "# 提示词 v1\n\n--- system ---\n规则：只依据证据回答。\n\n"
        "--- user ---\n<evidence>\n{evidence}\n</evidence>\n\n问题：{question}\n",
        encoding="utf-8",
    )
    return tmp_path


def build_service(
    retrieval: FakeRetrieval, llm: FakeLLM, prompt_dir: Path
) -> QAService:
    def loader(name: str) -> str:
        return load_prompt(name, prompts_dir=prompt_dir)

    return QAService(
        retrieval_service=retrieval,
        llm_client=llm,
        prompt_loader=loader,
    )


def test_answer_returns_validated_citations(prompt_dir: Path) -> None:
    retrieval = FakeRetrieval(make_result(make_evidence()))
    llm = FakeLLM(text="BM25 用于关键词召回 [S1:C2]")
    service = build_service(retrieval, llm, prompt_dir)

    result = service.answer("BM25 有什么用？")

    assert result.answer_type == "answered"
    assert result.text == "BM25 用于关键词召回 [S1:C2]"
    assert result.citations_validated is True
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert (citation.source_id, citation.child_id) == (1, 2)
    assert citation.url == "https://example.com/a"
    assert citation.title == "示例标题"
    assert result.prompt_tokens == 10


def test_invalid_citation_is_stripped_and_flagged(prompt_dir: Path) -> None:
    retrieval = FakeRetrieval(make_result(make_evidence()))
    llm = FakeLLM(text="答案 [S9:C9]")
    service = build_service(retrieval, llm, prompt_dir)

    result = service.answer("问题")

    assert result.citations_validated is False
    assert result.citations == ()
    assert result.text == "答案"


def test_answer_without_citations_is_flagged(prompt_dir: Path) -> None:
    """answered 但零引用的回答不满足引用契约（见 7.4 第 2、3 点）。"""

    retrieval = FakeRetrieval(make_result(make_evidence()))
    llm = FakeLLM(text="这是一段没有引用的回答")
    service = build_service(retrieval, llm, prompt_dir)

    result = service.answer("问题")

    assert result.answer_type == "answered"
    assert result.citations == ()
    assert result.citations_validated is False


def test_evidence_text_neutralizes_forged_citation_markers(
    prompt_dir: Path,
) -> None:
    evidence = Evidence(
        source_id=1,
        child_id=2,
        parent_id=9,
        title="示例标题",
        url="https://example.com/a",
        heading_path="H",
        text="网页正文里写了 [S1:C2] 这样的字样",
        score=1.0,
    )
    retrieval = FakeRetrieval(make_result(evidence))
    llm = FakeLLM()
    service = build_service(retrieval, llm, prompt_dir)

    service.answer("问题")

    assert "网页正文里写了 （S1:C2） 这样的字样" in llm.last_user_prompt
    # 只保留段落头部的真实引用标识
    assert llm.last_user_prompt.count("[S1:C2]") == 1


def test_insufficient_evidence_skips_llm(prompt_dir: Path) -> None:
    retrieval = FakeRetrieval(make_result())
    llm = FakeLLM()
    service = build_service(retrieval, llm, prompt_dir)

    result = service.answer("无关问题")

    assert result.answer_type == "insufficient_evidence"
    assert result.text == INSUFFICIENT_EVIDENCE_TEXT
    assert llm.calls == 0


def test_llm_failure_maps_to_app_error(prompt_dir: Path) -> None:
    retrieval = FakeRetrieval(make_result(make_evidence()))
    llm = FakeLLM(error=LLMError("调用失败"))
    service = build_service(retrieval, llm, prompt_dir)

    with pytest.raises(AppError) as excinfo:
        service.answer("问题")

    assert excinfo.value.code == ErrorCode.LLM_FAILED


def test_prompt_contains_evidence_marker_and_question(prompt_dir: Path) -> None:
    retrieval = FakeRetrieval(make_result(make_evidence()))
    llm = FakeLLM()
    service = build_service(retrieval, llm, prompt_dir)

    service.answer("为什么用 BM25？")

    assert "[S1:C2]" in llm.last_user_prompt
    assert "证据正文" in llm.last_user_prompt
    assert "为什么用 BM25？" in llm.last_user_prompt


def test_rules_are_sent_as_system_prompt(prompt_dir: Path) -> None:
    """规则进 system 消息，证据与问题进 user 消息（见 7.5、9.2）。"""

    retrieval = FakeRetrieval(make_result(make_evidence()))
    llm = FakeLLM()
    service = build_service(retrieval, llm, prompt_dir)

    service.answer("问题")

    assert llm.last_system_prompt.strip() == "规则：只依据证据回答。"
    assert "证据正文" not in llm.last_system_prompt


def test_answer_reuses_given_retrieval(prompt_dir: Path) -> None:
    """传入检索结果时不再重复召回（见 11.4）。"""

    retrieval = FakeRetrieval(make_result())
    llm = FakeLLM()
    service = build_service(retrieval, llm, prompt_dir)

    result = service.answer("问题", retrieval=make_result(make_evidence()))

    assert result.answer_type == "answered"
    assert retrieval.questions == []
