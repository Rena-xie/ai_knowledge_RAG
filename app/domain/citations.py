"""引用标识解析与服务端校验（见《项目开发说明_v1.md》7.4）。"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

CITATION_PATTERN = re.compile(r"\[S(\d+):C(\d+)\]")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)


@dataclass(frozen=True)
class Citation:
    """一条引用标识。"""

    source_id: int
    child_id: int


@dataclass(frozen=True)
class CitationValidation:
    """引用校验结果；text 为剔除无效引用后的回答。"""

    citations: tuple[Citation, ...]
    invalid: tuple[Citation, ...]
    text: str

    @property
    def citations_validated(self) -> bool:
        return not self.invalid


def format_citation(source_id: int, child_id: int) -> str:
    return f"[S{source_id}:C{child_id}]"


def extract_citations(text: str) -> tuple[Citation, ...]:
    """按出现顺序提取引用标识并去重。"""

    found: list[Citation] = []
    seen: set[Citation] = set()
    for match in CITATION_PATTERN.finditer(text):
        citation = Citation(int(match.group(1)), int(match.group(2)))
        if citation not in seen:
            seen.add(citation)
            found.append(citation)
    return tuple(found)


def validate_citations(
    text: str, allowed_pairs: Collection[tuple[int, int]]
) -> CitationValidation:
    """校验回答中的引用是否属于上下文集合，并剔除无效引用。"""

    allowed = {(source_id, child_id) for source_id, child_id in allowed_pairs}
    citations: list[Citation] = []
    invalid: list[Citation] = []
    for citation in extract_citations(text):
        if (citation.source_id, citation.child_id) in allowed:
            citations.append(citation)
        else:
            invalid.append(citation)

    cleaned = text
    for citation in invalid:
        cleaned = cleaned.replace(
            format_citation(citation.source_id, citation.child_id), ""
        )
    cleaned = _TRAILING_SPACE.sub("", _MULTI_SPACE.sub(" ", cleaned))
    return CitationValidation(tuple(citations), tuple(invalid), cleaned)
