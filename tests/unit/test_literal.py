"""查询字面量提取。"""

from dataclasses import dataclass

import pytest

from app.domain.literal import extract_literals
from app.domain.rrf import FusedHit
from app.services.retrieval import _promote_literal_matches


@dataclass
class _Chunk:
    text: str


def _rows(*texts: str) -> dict[int, tuple[_Chunk, object, object]]:
    return {
        index: (_Chunk(text), object(), object())
        for index, text in enumerate(texts, 1)
    }


def test_promote_orders_matches_by_bm25_rank() -> None:
    candidates = [FusedHit(1, 0.0, 1), FusedHit(2, 0.0, 2), FusedHit(3, 0.0, 3)]
    rows = _rows("print('x, y')", "print('x, y')", "无关内容")

    promoted = _promote_literal_matches(candidates, rows, ("x, y",), (2, 1, 3))

    assert [hit.child_id for hit in promoted] == [2, 1, 3]


def test_promote_falls_back_to_existing_order_without_bm25_rank() -> None:
    candidates = [FusedHit(1, 0.0, 1), FusedHit(2, 0.0, 2), FusedHit(3, 0.0, 3)]
    rows = _rows("print('x, y')", "print('x, y')", "无关内容")

    promoted = _promote_literal_matches(candidates, rows, ("x, y",), (3,))

    assert [hit.child_id for hit in promoted] == [1, 2, 3]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("print('hello, world') 出现在教程的哪一章？", ("hello, world",)),
        (
            "代码 open('/Users/michael/test.txt', 'r') 在讲什么？",
            ("/Users/michael/test.txt",),
        ),
        ("FastAPI 的 /items/ 路由示例里 skip 默认值是多少？", ("/items/",)),
        ("`skip` 参数的默认值？", ("skip",)),
        ("先看 'abcd' 再看 'abcdef'", ("abcdef", "abcd")),
        ("sorted() 的 key 参数怎么用？", ()),
        ("LangGraph 中 interrupt() 暂停后怎么恢复？", ()),
        ('怎么理解 "检索增强" 这个词？', ()),
        ("普通问题，没有代码片段", ()),
    ],
)
def test_extract_literals(question: str, expected: tuple[str, ...]) -> None:
    assert extract_literals(question) == expected
