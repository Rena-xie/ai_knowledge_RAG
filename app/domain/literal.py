"""提取查询中的逐字代码片段，用于字面量优先排序。"""

from __future__ import annotations

import re

_QUOTED = re.compile(r"`([^`]{3,})`|'([^'\n]{3,})'|\"([^\"\n]{3,})\"")
_PATH = re.compile(r"(?<![\w/])/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]*)+")
_CJK = re.compile(r"[　-〿一-鿿＀-￯]")


def extract_literals(question: str) -> tuple[str, ...]:
    """返回查询中需要逐字匹配的代码片段，按长度降序。

    只识别引号包裹的 ASCII 内容与 ``/path/`` 形式，中文引号或短语不算，
    避免自然语言问题被当成字面量查询。
    """

    found: list[str] = []
    for match in _QUOTED.finditer(question):
        content = next(group for group in match.groups() if group is not None)
        content = content.strip()
        if len(content) >= 3 and not _CJK.search(content):
            found.append(content)
    for match in _PATH.finditer(question):
        found.append(match.group(0))
    return tuple(sorted(dict.fromkeys(found), key=len, reverse=True))
