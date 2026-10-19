"""问答服务：证据拼装、引用校验与证据不足处理（见《项目开发说明_v1.md》7.4、7.5）。"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.core.errors import AppError, ErrorCode
from app.domain.citations import validate_citations
from app.infra.llm import LLMClient, LLMError, load_prompt, split_prompt
from app.services.retrieval import Evidence, RetrievalResult, RetrievalService

INSUFFICIENT_EVIDENCE_TEXT = "知识库缺少相关证据。"
ANSWER_TYPE_ANSWERED = "answered"
ANSWER_TYPE_INSUFFICIENT = "insufficient_evidence"
CITATION_LIKE_PATTERN = re.compile(r"\[S\d+:C\d+\]")
EVIDENCE_BOUNDARY_PATTERN = re.compile(
    r"<\s*/?\s*evidence\s*>", re.IGNORECASE
)


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
    （见《项目开发说明_v1.md》7.4 第 1 点）。标题、URL、标题路径和正文中的
    `<evidence>` 边界也会被中和，避免不可信网页内容提前闭合证据块。
    """

    title = _sanitize_evidence_field(evidence.title or "未命名")
    url = _sanitize_evidence_field(evidence.url)
    heading_path = _sanitize_evidence_field(evidence.heading_path)
    body = _sanitize_evidence_field(evidence.text)
    header = (
        f"[S{evidence.source_id}:C{evidence.child_id}] "
        f"{title} | {url} | {heading_path}"
    )
    return f"{header}\n{body}"


def _neutralize_citations(text: str) -> str:
    """把证据正文中的引用标识改写成不可解析形式。"""

    return CITATION_LIKE_PATTERN.sub(
        lambda match: f"（{match.group(0)[1:-1]}）", text
    )


def _neutralize_evidence_boundaries(text: str) -> str:
    """把内容中的 `<evidence>` / `</evidence>` 改写成普通文本。"""

    return EVIDENCE_BOUNDARY_PATTERN.sub(
        lambda match: f"（{match.group(0)[1:-1]}）", text
    )


def _sanitize_evidence_field(text: str) -> str:
    return _neutralize_citations(_neutralize_evidence_boundaries(text))


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

    def answer(
        self, question: str, *, retrieval: RetrievalResult | None = None
    ) -> AnswerResult:
        """生成带引用的回答；传入 ``retrieval`` 时复用已有检索结果，不重复召回。"""

        result = retrieval if retrieval is not None else self._retrieval_service.search(
            question
        )
        if not result.evidences:
            return AnswerResult(
                text=INSUFFICIENT_EVIDENCE_TEXT,
                answer_type=ANSWER_TYPE_INSUFFICIENT,
                citations=(),
                citations_validated=True,
            )

        by_id = {
            (evidence.source_id, evidence.child_id): evidence
            for evidence in result.evidences
        }
        system_prompt, user_template = split_prompt(
            self._prompt_loader(self._prompt_name)
        )
        rendered = self._render_prompt(user_template, question, result.evidences)
        try:
            llm_result = self._llm_client.complete(
                system_prompt=system_prompt, user_prompt=rendered
            )
        except LLMError as error:
            raise AppError(ErrorCode.LLM_FAILED, str(error)) from error

        validation = validate_citations(llm_result.text, set(by_id))
        prompt_tokens = llm_result.prompt_tokens
        completion_tokens = llm_result.completion_tokens
        if not validation.citations or not validation.citations_validated:
            retry_prompt = (
                rendered
                + "\n\n【引用修复】上一次回答缺少有效引用。"
                "请重新回答，并确保每个关键结论后至少有一个来自 "
                "<evidence> 的引用，格式严格为 [S{source_id}:C{child_id}]。"
            )
            try:
                retry_result = self._llm_client.complete(
                    system_prompt=system_prompt, user_prompt=retry_prompt
                )
            except LLMError:
                return AnswerResult(
                    text=INSUFFICIENT_EVIDENCE_TEXT,
                    answer_type=ANSWER_TYPE_INSUFFICIENT,
                    citations=(),
                    citations_validated=True,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            prompt_tokens += retry_result.prompt_tokens
            completion_tokens += retry_result.completion_tokens
            retry_validation = validate_citations(
                retry_result.text, set(by_id)
            )
            if (
                retry_validation.citations
                and retry_validation.citations_validated
            ):
                validation = retry_validation
            else:
                return AnswerResult(
                    text=INSUFFICIENT_EVIDENCE_TEXT,
                    answer_type=ANSWER_TYPE_INSUFFICIENT,
                    citations=(),
                    citations_validated=True,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
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
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    @staticmethod
    def _render_prompt(
        template: str, question: str, evidences: Sequence[Evidence]
    ) -> str:
        evidence_text = "\n\n".join(format_evidence(item) for item in evidences)
        safe_question = _neutralize_evidence_boundaries(question)
        return template.replace("{evidence}", evidence_text).replace(
            "{question}", safe_question
        )
