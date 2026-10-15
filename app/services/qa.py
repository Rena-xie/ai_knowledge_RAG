"""问答服务：证据拼装、引用校验与证据不足处理（见《项目介绍.md》7.4、7.5）。"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.core.errors import AppError, ErrorCode
from app.domain.citations import validate_citations
from app.infra.llm import LLMClient, LLMError, load_prompt
from app.services.retrieval import Evidence, RetrievalService

INSUFFICIENT_EVIDENCE_TEXT = "知识库缺少相关证据。"
ANSWER_TYPE_ANSWERED = "answered"
ANSWER_TYPE_INSUFFICIENT = "insufficient_evidence"
SYSTEM_PROMPT = "你是 AI 知识库的问答助手，严格遵守用户消息中的规则。"
CITATION_LIKE_PATTERN = re.compile(r"\[S\d+:C\d+\]")


@dataclass(frozen=True)
class AnswerCitation:
    """回答中通过校验的引用。"""

    source_id: int
    child_id: int
    title: str | None
    url: str
    heading_path: str


@dataclass(frozen=True)
class AnswerResult:
    """问答结果。"""

    text: str
    answer_type: str
    citations: tuple[AnswerCitation, ...]
    citations_validated: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0


def format_evidence(evidence: Evidence) -> str:
    """把证据渲染为带引用标识的上下文段落。

    正文中形如 `[S1:C2]` 的字样会被中和，避免网页内容伪造可校验的引用标识
    （见《项目介绍.md》7.4 第 1 点）。
    """

    title = evidence.title or "未命名"
    header = (
        f"[S{evidence.source_id}:C{evidence.child_id}] "
        f"{title} | {evidence.url} | {evidence.heading_path}"
    )
    return f"{header}\n{_neutralize_citations(evidence.text)}"


def _neutralize_citations(text: str) -> str:
    """把证据正文中的引用标识改写成不可解析形式。"""

    return CITATION_LIKE_PATTERN.sub(
        lambda match: f"（{match.group(0)[1:-1]}）", text
    )


class QAService:
    """组织证据与提示词，调用 LLM 并校验引用。"""

    def __init__(
        self,
        *,
        retrieval_service: RetrievalService,
        llm_client: LLMClient,
        prompt_name: str = "system_prompt_v1",
        prompt_loader: Callable[[str], str] = load_prompt,
    ) -> None:
        self._retrieval_service = retrieval_service
        self._llm_client = llm_client
        self._prompt_name = prompt_name
        self._prompt_loader = prompt_loader

    def answer(self, question: str) -> AnswerResult:
        retrieval = self._retrieval_service.search(question)
        if not retrieval.evidences:
            return AnswerResult(
                text=INSUFFICIENT_EVIDENCE_TEXT,
                answer_type=ANSWER_TYPE_INSUFFICIENT,
                citations=(),
                citations_validated=True,
            )

        by_id = {
            (evidence.source_id, evidence.child_id): evidence
            for evidence in retrieval.evidences
        }
        rendered = self._render_prompt(question, retrieval.evidences)
        try:
            llm_result = self._llm_client.complete(
                system_prompt=SYSTEM_PROMPT, user_prompt=rendered
            )
        except LLMError as error:
            raise AppError(ErrorCode.LLM_FAILED, str(error)) from error

        validation = validate_citations(llm_result.text, set(by_id))
        citations = tuple(
            AnswerCitation(
                source_id=citation.source_id,
                child_id=citation.child_id,
                title=by_id[(citation.source_id, citation.child_id)].title,
                url=by_id[(citation.source_id, citation.child_id)].url,
                heading_path=by_id[
                    (citation.source_id, citation.child_id)
                ].heading_path,
            )
            for citation in validation.citations
        )
        return AnswerResult(
            text=validation.text,
            answer_type=ANSWER_TYPE_ANSWERED,
            citations=citations,
            # answered 的回答必须至少包含一个有效引用（见 7.4 第 2、3 点）
            citations_validated=validation.citations_validated and bool(citations),
            prompt_tokens=llm_result.prompt_tokens,
            completion_tokens=llm_result.completion_tokens,
        )

    def _render_prompt(self, question: str, evidences: Sequence[Evidence]) -> str:
        template = self._prompt_loader(self._prompt_name)
        evidence_text = "\n\n".join(format_evidence(item) for item in evidences)
        return template.replace("{evidence}", evidence_text).replace(
            "{question}", question
        )
