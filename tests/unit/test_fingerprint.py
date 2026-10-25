"""正文指纹规范化。"""

from app.domain.fingerprint import (
    FINGERPRINT_VERSION,
    compute_content_fingerprint,
    normalize_markdown,
)


def test_ignores_line_endings_and_trailing_spaces() -> None:
    first = "# 标题\r\n\r\n正文  \r\n"
    second = "# 标题\n\n正文"

    assert normalize_markdown(first) == normalize_markdown(second)
    assert compute_content_fingerprint(first) == compute_content_fingerprint(second)


def test_preserves_internal_whitespace_and_code_indentation() -> None:
    first = "```python\nif True:\n    pass\n```"
    second = "```python\nif True:\n pass\n```"

    assert compute_content_fingerprint(first) != compute_content_fingerprint(second)


def test_is_deterministic_for_unicode() -> None:
    first = compute_content_fingerprint("# 中文\n内容")
    second = compute_content_fingerprint("# 中文\n内容")

    assert first == second
    assert first.version == FINGERPRINT_VERSION
    assert len(first.value) == 64
