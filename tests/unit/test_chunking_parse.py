"""Markdown 结构解析（见《项目介绍.md》6.1）。"""

from app.domain.chunking import Block, parse_blocks

SAMPLE = """# RAG 基础

RAG 把检索和生成结合起来。

## 检索

BM25 对专有名词敏感。

- 召回
- 排序

```python
def bm25():
    return 1
```

| 参数 | 说明 |
| --- | --- |
| k | 平滑常数 |

#### 四级标题不入结构
"""


def test_parses_blocks_in_document_order() -> None:
    blocks = parse_blocks(SAMPLE)

    assert all(isinstance(block, Block) for block in blocks)
    assert [block.content_type for block in blocks] == [
        "paragraph",
        "paragraph",
        "list",
        "code",
        "table",
        "paragraph",
    ]


def test_tracks_heading_path() -> None:
    blocks = parse_blocks(SAMPLE)

    assert blocks[0].heading_path == "RAG 基础"
    assert blocks[1].heading_path == "RAG 基础 / 检索"
    assert blocks[5].heading_path == "RAG 基础 / 检索"
    assert blocks[5].text == "#### 四级标题不入结构"


def test_keeps_block_text_intact() -> None:
    blocks = parse_blocks(SAMPLE)

    assert blocks[0].text == "RAG 把检索和生成结合起来。"
    assert blocks[2].text == "- 召回\n- 排序"
    assert blocks[3].text == "def bm25():\n    return 1"
    assert blocks[4].text.startswith("| 参数 | 说明 |")


def test_code_block_keeps_internal_blank_lines() -> None:
    blocks = parse_blocks("```\nline1\n\nline2\n```\n")

    assert blocks[0].content_type == "code"
    assert blocks[0].text == "line1\n\nline2"


def test_ordered_list_is_a_list_block() -> None:
    blocks = parse_blocks("1. 第一\n2. 第二\n")

    assert blocks[0].content_type == "list"
    assert blocks[0].text == "1. 第一\n2. 第二"


def test_paragraph_merges_soft_wrapped_lines() -> None:
    blocks = parse_blocks("第一行\n第二行\n\n第三段\n")

    assert len(blocks) == 2
    assert blocks[0].text == "第一行\n第二行"
    assert blocks[1].text == "第三段"


def test_heading_only_document_has_no_blocks() -> None:
    assert parse_blocks("# 只有标题\n") == []
