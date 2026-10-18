"""正文解析（见《项目开发说明_v1.md》6.1）。"""

import pytest

from app.core.errors import ErrorCode
from app.domain.chunking import chunk_markdown
from app.infra.parser import ParseError, parse_html

ARTICLE = """
<html>
  <head><title>BM25 指南</title></head>
  <body>
    <article>
      <h1>BM25 指南</h1>
      <p>BM25 是一种稀疏检索算法，通过对词频和文档长度进行归一化
      来计算查询与文档的相关性得分。</p>
      <h2>适用场景</h2>
      <p>当查询包含专有名词、产品名或代码函数名时，BM25 往往比纯向量检索更准确，
      因此常与稠密检索组合使用。</p>
      <p>在混合检索系统中，两路召回结果通常使用倒数排名融合算法合并，从而同时兼顾语义召回与关键词召回。</p>
      <p>工程实现上需要为中文查询准备分词方案，并把分词结果写入全文索引，使关键词匹配能够覆盖中文表达。</p>
      <h2>参数选择</h2>
      <p>常见的平滑常数取值来自原论文的实验结论，实际项目里应当结合评测集重新校准，而不是照搬默认值。</p>
      <p>切块长度同样会影响召回效果，过长的切块会稀释关键词权重，过短则会损失上下文信息。</p>
    </article>
  </body>
</html>
"""


def test_extracts_markdown_and_title() -> None:
    document = parse_html(ARTICLE, url="https://example.com/bm25")

    assert "BM25" in document.markdown
    assert "稀疏检索" in document.markdown
    assert document.title == "BM25 指南"


def test_output_can_be_chunked() -> None:
    document = parse_html(ARTICLE)

    parents = chunk_markdown(
        document.markdown,
        parent_max_chars=3000,
        child_target_chars=900,
        child_max_chars=1000,
        child_overlap_chars=100,
    )

    assert parents
    assert any("BM25" in child.text for parent in parents for child in parent.children)


def test_raises_on_empty_content() -> None:
    with pytest.raises(ParseError) as excinfo:
        parse_html("<html><body></body></html>")

    assert excinfo.value.code == ErrorCode.PARSE_EMPTY_CONTENT
