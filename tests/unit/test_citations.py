"""引用标识解析与服务端校验（见《项目介绍.md》7.4）。"""

from app.domain.citations import (
    Citation,
    extract_citations,
    format_citation,
    validate_citations,
)


def test_extract_citations_keeps_order_and_deduplicates() -> None:
    text = "结论 A [S1:C2]，结论 B [S3:C4]，再次 [S1:C2]"

    assert extract_citations(text) == (Citation(1, 2), Citation(3, 4))


def test_format_citation_round_trips() -> None:
    marker = format_citation(12, 57)

    assert marker == "[S12:C57]"
    assert extract_citations(marker) == (Citation(12, 57),)


def test_validate_citations_splits_valid_and_invalid() -> None:
    result = validate_citations("好 [S1:C2] 坏 [S9:C9]", {(1, 2), (3, 4)})

    assert result.citations == (Citation(1, 2),)
    assert result.invalid == (Citation(9, 9),)
    assert result.citations_validated is False


def test_validate_citations_removes_invalid_markers_from_text() -> None:
    result = validate_citations("结论 [S1:C2] 与 [S9:C9]", {(1, 2)})

    assert result.text == "结论 [S1:C2] 与"


def test_all_valid_citations_are_flagged_validated() -> None:
    result = validate_citations("只有 [S1:C2]", {(1, 2)})

    assert result.citations_validated is True
    assert result.text == "只有 [S1:C2]"


def test_text_without_citations_is_validated() -> None:
    result = validate_citations("没有引用", {(1, 2)})

    assert result.citations == ()
    assert result.invalid == ()
    assert result.citations_validated is True
