"""基于正文字符比例的确定性语言分类。"""

from __future__ import annotations

import re

LANGUAGE_ZH = "zh"
LANGUAGE_EN = "en"
LANGUAGE_MIXED = "mixed"
LANGUAGE_UNKNOWN = "unknown"

MIN_LANGUAGE_CHARS = 20
DOMINANT_LANGUAGE_RATIO = 0.6

_FENCED_CODE_RE = re.compile(r"(?ms)^```[^\n]*\n.*?^```[ \t]*$")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _prose(markdown: str) -> str:
    without_fences = _FENCED_CODE_RE.sub(" ", markdown)
    return _INLINE_CODE_RE.sub(" ", without_fences)


def language_character_counts(markdown: str) -> tuple[int, int]:
    """返回正文中的 CJK 与拉丁字母字符数。"""

    prose = _prose(markdown)
    return (
        len(_CJK_RE.findall(prose)),
        len(_LATIN_RE.findall(prose)),
    )


def detect_language(markdown: str) -> str:
    """返回 zh、en、mixed 或 unknown，不产生外部调用。"""

    cjk_count, latin_count = language_character_counts(markdown)
    total = cjk_count + latin_count
    if total < MIN_LANGUAGE_CHARS:
        return LANGUAGE_UNKNOWN
    cjk_ratio = cjk_count / total
    latin_ratio = latin_count / total
    if cjk_ratio >= DOMINANT_LANGUAGE_RATIO:
        return LANGUAGE_ZH
    if latin_ratio >= DOMINANT_LANGUAGE_RATIO:
        return LANGUAGE_EN
    if cjk_count and latin_count:
        return LANGUAGE_MIXED
    return LANGUAGE_UNKNOWN
