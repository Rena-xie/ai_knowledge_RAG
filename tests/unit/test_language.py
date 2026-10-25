"""确定性语言元数据测试。"""

from app.domain.language import detect_language


def test_detects_chinese_even_with_english_technical_terms() -> None:
    markdown = (
        "知识库需要保存文档正文并返回可追溯证据，BM25 用于关键词检索。"
        "每个来源都保留最终 URL，正文切块后写入本地索引。"
    )

    assert detect_language(markdown) == "zh"


def test_detects_english() -> None:
    markdown = (
        "The knowledge base stores document text and traceable evidence. "
        "Each source keeps its final URL before chunking."
    )

    assert detect_language(markdown) == "en"


def test_detects_mixed_language_prose() -> None:
    markdown = (
        "这个知识库保存中文文档并返回可追溯证据。中文和英文资料使用同一套"
        "检索流程，系统保留来源、标题路径和正文片段。每个结果都能回溯到原始"
        "网页，维护者可以分别查看两种语言的质量指标。"
        "The knowledge base also stores English documents and retrieves "
        "relevant passages for downstream agents."
    )

    assert detect_language(markdown) == "mixed"


def test_short_or_empty_content_is_unknown() -> None:
    assert detect_language("") == "unknown"
    assert detect_language("RAG 知识库") == "unknown"


def test_code_does_not_override_chinese_prose() -> None:
    markdown = """
知识库使用确定的语言元数据，并且不会根据代码块中的英文标识符改变正文语言。
文档正文仍然使用中文，代码只作为示例保留。

```python
from fastapi import FastAPI
knowledge_base_service = FastAPI()
```
"""

    assert detect_language(markdown) == "zh"


def test_inline_code_is_ignored() -> None:
    markdown = (
        "中文正文说明如何使用 `dependency_injection_container`，"
        "并保留 `traceable_evidence_payload` 作为示例。语言判断只统计正文。"
    )

    assert detect_language(markdown) == "zh"
