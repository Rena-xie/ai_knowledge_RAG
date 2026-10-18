"""正文解析（见《项目开发说明_v1.md》6.1）。"""

from __future__ import annotations

from dataclasses import dataclass

import trafilatura

from app.core.errors import ErrorCode


class ParseError(Exception):
    """正文提取失败。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ParsedDocument:
    """解析结果：标题与 Markdown 正文。"""

    title: str | None
    markdown: str


def parse_html(html: str, *, url: str | None = None) -> ParsedDocument:
    """把 HTML 提取为 Markdown 正文与标题。"""

    markdown = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=False,
        include_images=False,
        include_formatting=True,
        include_comments=False,
    )
    if not markdown or not markdown.strip():
        raise ParseError(ErrorCode.PARSE_EMPTY_CONTENT, "未提取到有效正文")

    title: str | None = None
    metadata = trafilatura.extract_metadata(html)
    if metadata is not None and metadata.title:
        title = metadata.title.strip() or None
    return ParsedDocument(title=title, markdown=markdown.strip())
