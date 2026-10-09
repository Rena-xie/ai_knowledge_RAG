"""Parent/Child 切分规则（见《项目介绍.md》6.2–6.4）。"""

import pytest

from app.domain.chunking import chunk_markdown

PARAMS = {
    "parent_max_chars": 300,
    "child_target_chars": 120,
    "child_max_chars": 160,
    "child_overlap_chars": 30,
}


def test_small_document_has_single_parent_and_child() -> None:
    parents = chunk_markdown("# 标题\n\n短文。\n", **PARAMS)

    assert len(parents) == 1
    parent = parents[0]
    assert parent.heading_path == "标题"
    assert parent.content == "短文。"
    assert len(parent.children) == 1
    child = parent.children[0]
    assert child.text == "短文。"
    assert child.heading_path == "标题"
    assert child.content_type == "paragraph"
    assert child.embedding_text == "标题\n短文。"


def test_parents_split_by_heading_boundaries() -> None:
    parents = chunk_markdown("# A\n\n甲\n\n# B\n\n乙\n", **PARAMS)

    assert [parent.heading_path for parent in parents] == ["A", "B"]
    assert [parent.ordinal for parent in parents] == [0, 1]


def test_children_do_not_cross_parent_boundary() -> None:
    markdown = "# A\n\n" + "甲" * 200 + "\n\n# B\n\n乙\n"

    parents = chunk_markdown(markdown, **PARAMS)

    assert len(parents[0].children) >= 2
    assert parents[1].children[0].text == "乙"


def test_long_paragraph_is_split_within_max_length() -> None:
    markdown = "# 标题\n\n" + "句子很长。" * 60 + "\n"

    parents = chunk_markdown(markdown, **PARAMS)
    children = [child for parent in parents for child in parent.children]

    assert len(children) >= 3
    assert all(len(child.text) <= PARAMS["child_max_chars"] for child in children)


def test_code_block_is_kept_intact_even_when_longer_than_max() -> None:
    code = "```\n" + "x = 1\n" * 60 + "```\n"

    parents = chunk_markdown("# 标题\n\n" + code, **PARAMS)
    code_children = [
        child
        for parent in parents
        for child in parent.children
        if child.content_type == "code"
    ]

    assert len(code_children) == 1
    assert len(code_children[0].text) > PARAMS["child_max_chars"]


def test_overlap_applies_within_one_parent_only() -> None:
    markdown = "# A\n\n" + "甲乙丙丁。" * 50 + "\n\n# B\n\n第二段内容。\n"

    parents = chunk_markdown(markdown, **PARAMS)
    first_parent_children = parents[0].children

    assert len(first_parent_children) >= 2
    overlap_prefix = first_parent_children[0].text[-PARAMS["child_overlap_chars"] :]
    assert first_parent_children[1].text.startswith(overlap_prefix)
    assert not parents[1].children[0].text.startswith(overlap_prefix)


def test_chunking_is_deterministic() -> None:
    markdown = "# 标题\n\n" + "内容。" * 50 + "\n"

    assert chunk_markdown(markdown, **PARAMS) == chunk_markdown(markdown, **PARAMS)


def test_invalid_child_params_are_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_markdown(
            "# A\n\n内容\n",
            parent_max_chars=100,
            child_target_chars=200,
            child_max_chars=100,
            child_overlap_chars=10,
        )
