"""正文解析（见《项目开发说明_v1.md》6.1）。"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass

import trafilatura

from app.core.errors import ErrorCode

_PRE_BLOCK_RE = re.compile(
    r"<pre\b[^>]*>(.*?)</pre>",
    re.IGNORECASE | re.DOTALL,
)
_INLINE_WRAPPER_RE = re.compile(
    r"</?(?:span|font|sup|sub|small|mark|kbd|abbr|strong|em|code)"
    r"\b[^>]*>",
    re.IGNORECASE,
)
_CODE_WRAPPER_RE = re.compile(r"</?(?:span|font)\b[^>]*>", re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_FOOTNOTE_MARK_RE = re.compile(r"¶")
_WHITESPACE_RE = re.compile(r"\s+")


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


@dataclass(frozen=True)
class _PreparedHtml:
    html: str
    title: str | None
    code_blocks: tuple[str, ...]


def _clean_text(value: str) -> str:
    text = html_module.unescape(_HTML_TAG_RE.sub("", value))
    text = _FOOTNOTE_MARK_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _extract_heading(html: str) -> str | None:
    match = _H1_RE.search(html)
    if match is None:
        return None
    title = _clean_text(match.group(1))
    return title or None


def _normalize_code(code: str) -> str:
    normalized = code.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _code_fence(code: str) -> str:
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", code)),
        default=0,
    )
    return "`" * max(3, longest_run + 1)


def _protect_code_blocks(html: str) -> tuple[str, tuple[str, ...]]:
    code_blocks: list[str] = []

    def replace(match: re.Match[str]) -> str:
        raw_code = match.group(1)
        text = html_module.unescape(_HTML_TAG_RE.sub("", raw_code))
        text = _CODE_WRAPPER_RE.sub("", text)
        code = _normalize_code(text)
        code_blocks.append(code)
        return f"<p>CQCODEBLOCK{len(code_blocks) - 1:04d}</p>"

    return _PRE_BLOCK_RE.sub(replace, html), tuple(code_blocks)


def _prepare_html(html: str) -> _PreparedHtml:
    heading = _extract_heading(html)
    protected, code_blocks = _protect_code_blocks(html)
    return _PreparedHtml(
        html=protected,
        title=heading,
        code_blocks=code_blocks,
    )


def _strip_inline_wrappers(markdown: str) -> str:
    without_breaks = _BR_RE.sub("\n", markdown)
    without_tags = _INLINE_WRAPPER_RE.sub("", without_breaks)
    return _FOOTNOTE_MARK_RE.sub("", without_tags)


def _restore_code_blocks(
    markdown: str,
    code_blocks: tuple[str, ...],
) -> str:
    restored = markdown
    for index, code in enumerate(code_blocks):
        token = f"CQCODEBLOCK{index:04d}"
        if token not in restored:
            continue
        fence = _code_fence(code)
        replacement = f"\n\n{fence}\n{code}\n{fence}\n\n"
        restored = restored.replace(token, replacement)
    return restored


def _metadata_title(html: str) -> str | None:
    metadata = trafilatura.extract_metadata(html)
    if metadata is None or not metadata.title:
        return None
    return _clean_text(metadata.title) or None


def _extract_markdown(html: str, url: str | None) -> str:
    markdown = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=False,
        include_images=False,
        include_formatting=True,
        include_comments=False,
        include_tables=True,
    )
    if not markdown or not markdown.strip():
        raise ParseError(ErrorCode.PARSE_EMPTY_CONTENT, "未提取到有效正文")
    return markdown


def _assemble_document(
    html: str,
    prepared: _PreparedHtml,
    markdown: str,
) -> ParsedDocument:
    cleaned = _strip_inline_wrappers(markdown)
    restored = _restore_code_blocks(cleaned, prepared.code_blocks)
    title = prepared.title or _metadata_title(html)
    return ParsedDocument(title=title, markdown=restored.strip())


def parse_html(html: str, *, url: str | None = None) -> ParsedDocument:
    """把 HTML 提取为 Markdown 正文与标题。"""

    prepared = _prepare_html(html)
    markdown = _extract_markdown(prepared.html, url)
    return _assemble_document(html, prepared, markdown)
