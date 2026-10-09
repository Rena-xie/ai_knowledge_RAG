"""Markdown 结构解析与 Parent/Child 切块（见《项目介绍.md》6.1–6.4）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby

INTACT_CONTENT_TYPES = frozenset({"code", "table"})
TEXT_CONTENT_TYPES = frozenset({"paragraph", "list"})
RECURSIVE_SEPARATORS = (
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    "；",
    ". ",
    "! ",
    "? ",
    "; ",
    "，",
    ", ",
    " ",
)
LIST_MARKERS = ("- ", "* ", "+ ")
HEADING_LIMIT = 3


@dataclass(frozen=True)
class Block:
    """带标题路径的结构化内容块。"""

    text: str
    content_type: str
    heading_path: str
    section: int = 0


@dataclass(frozen=True)
class ChildChunk:
    """Parent 内部的可检索切片。"""

    ordinal: int
    content_type: str
    heading_path: str
    text: str
    embedding_text: str


@dataclass(frozen=True)
class ParentChunk:
    """章节级切块，children 为其中的检索切片。"""

    ordinal: int
    heading_path: str
    content: str
    children: tuple[ChildChunk, ...]


def _heading_level(line: str) -> int | None:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return None
    hashes = len(stripped) - len(stripped.lstrip("#"))
    if hashes > HEADING_LIMIT or len(stripped) <= hashes or stripped[hashes] != " ":
        return None
    return hashes


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _is_list_line(line: str) -> bool:
    stripped = line.lstrip()
    if stripped.startswith(LIST_MARKERS):
        return True
    head, _, _rest = stripped.partition(" ")
    return len(head) > 1 and head[-1] in ".)" and head[:-1].isdigit()


def _join_path(stack: Sequence[str]) -> str:
    return " / ".join(stack)


def parse_blocks(markdown: str) -> list[Block]:
    """把 Markdown 解析为带标题路径的有序内容块。"""

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    heading_stack: list[str] = []
    section = 0
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        level = _heading_level(line)
        if level is not None:
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(line.strip().lstrip("#").strip())
            section += 1
            index += 1
            continue

        heading_path = _join_path(heading_stack)

        if line.lstrip().startswith("```"):
            index += 1
            code_lines: list[str] = []
            while index < total and not lines[index].lstrip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            index += 1
            text = "\n".join(code_lines).strip("\n")
            if text:
                blocks.append(Block(text, "code", heading_path, section))
            continue

        if _is_table_line(line):
            table_lines: list[str] = []
            while index < total and _is_table_line(lines[index]):
                table_lines.append(lines[index].rstrip())
                index += 1
            blocks.append(Block("\n".join(table_lines), "table", heading_path, section))
            continue

        if _is_list_line(line):
            list_lines: list[str] = []
            while index < total and lines[index].strip():
                candidate = lines[index]
                if not (_is_list_line(candidate) or candidate.startswith(("  ", "\t"))):
                    break
                list_lines.append(candidate.rstrip())
                index += 1
            blocks.append(Block("\n".join(list_lines), "list", heading_path, section))
            continue

        paragraph_lines: list[str] = []
        while index < total and lines[index].strip():
            candidate = lines[index]
            if (
                _heading_level(candidate) is not None
                or _is_table_line(candidate)
                or _is_list_line(candidate)
                or candidate.lstrip().startswith("```")
            ):
                break
            paragraph_lines.append(candidate.rstrip())
            index += 1
        blocks.append(
            Block("\n".join(paragraph_lines), "paragraph", heading_path, section)
        )

    return blocks


def _hard_split(text: str, max_chars: int) -> list[str]:
    pieces = []
    for start in range(0, len(text), max_chars):
        piece = text[start : start + max_chars].strip("\n")
        if piece:
            pieces.append(piece)
    return pieces


def _split_text(text: str, *, target: int, max_chars: int) -> list[str]:
    text = text.strip("\n")
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] | None = None
    for separator in RECURSIVE_SEPARATORS:
        if separator in text:
            parts = text.split(separator)
            rejoined = [part + separator for part in parts[:-1]]
            if parts[-1]:
                rejoined.append(parts[-1])
            pieces = rejoined
            break
    if pieces is None:
        pieces = [text]

    merged: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > target:
            merged.append(current)
            current = piece
        else:
            current += piece
    if current:
        merged.append(current)

    result: list[str] = []
    for item in merged:
        if len(item) <= max_chars:
            result.append(item.strip("\n"))
        else:
            result.extend(_hard_split(item, max_chars))
    return [item for item in result if item]


def _embedding_text(heading_path: str, text: str) -> str:
    return f"{heading_path}\n{text}" if heading_path else text


def _build_children(
    blocks: Sequence[Block], *, target: int, max_chars: int, overlap: int
) -> list[ChildChunk]:
    heading_path = blocks[0].heading_path
    raw: list[tuple[str, str]] = []
    buffer_type: str | None = None
    buffer_texts: list[str] = []

    def flush() -> None:
        nonlocal buffer_type, buffer_texts
        if buffer_type is not None and buffer_texts:
            joiner = "\n\n" if buffer_type == "paragraph" else "\n"
            joined = joiner.join(buffer_texts)
            for piece in _split_text(joined, target=target, max_chars=max_chars):
                raw.append((buffer_type, piece))
        buffer_type = None
        buffer_texts = []

    for block in blocks:
        if block.content_type in INTACT_CONTENT_TYPES:
            flush()
            raw.append((block.content_type, block.text))
            continue
        if buffer_type is not None and buffer_type != block.content_type:
            flush()
        buffer_type = block.content_type
        buffer_texts.append(block.text)
    flush()

    children: list[ChildChunk] = []
    previous_text = ""
    previous_type: str | None = None
    for content_type, text in raw:
        if (
            overlap > 0
            and previous_text
            and previous_type in TEXT_CONTENT_TYPES
            and content_type in TEXT_CONTENT_TYPES
        ):
            room = max_chars - len(text)
            if room > 0:
                prefix = previous_text[-min(overlap, room) :]
                if prefix and not text.startswith(prefix):
                    text = prefix + text
        children.append(
            ChildChunk(
                ordinal=len(children),
                content_type=content_type,
                heading_path=heading_path,
                text=text,
                embedding_text=_embedding_text(heading_path, text),
            )
        )
        previous_text = text
        previous_type = content_type
    return children


def _group_for_parents(
    blocks: Sequence[Block], parent_max_chars: int
) -> list[list[Block]]:
    groups: list[list[Block]] = []
    current: list[Block] = []
    current_length = 0
    for block in blocks:
        separator = 2 if current else 0
        if current and current_length + separator + len(block.text) > parent_max_chars:
            groups.append(current)
            current = [block]
            current_length = len(block.text)
        else:
            current.append(block)
            current_length += separator + len(block.text)
    if current:
        groups.append(current)
    return groups


def chunk_markdown(
    markdown: str,
    *,
    parent_max_chars: int,
    child_target_chars: int,
    child_max_chars: int,
    child_overlap_chars: int,
) -> list[ParentChunk]:
    """按章节切 Parent，并在 Parent 内部切 Child。"""

    if parent_max_chars <= 0:
        raise ValueError("parent_max_chars 必须为正数")
    if child_target_chars <= 0 or child_max_chars < child_target_chars:
        raise ValueError("child_max_chars 必须不小于 child_target_chars")
    if child_overlap_chars < 0 or child_overlap_chars >= child_max_chars:
        raise ValueError("child_overlap_chars 必须在 [0, child_max_chars) 范围内")

    parents: list[ParentChunk] = []
    for _section, section_blocks in groupby(
        parse_blocks(markdown), key=lambda block: block.section
    ):
        for parent_blocks in _group_for_parents(list(section_blocks), parent_max_chars):
            children = _build_children(
                parent_blocks,
                target=child_target_chars,
                max_chars=child_max_chars,
                overlap=child_overlap_chars,
            )
            if not children:
                continue
            parents.append(
                ParentChunk(
                    ordinal=len(parents),
                    heading_path=parent_blocks[0].heading_path,
                    content="\n\n".join(block.text for block in parent_blocks),
                    children=tuple(children),
                )
            )
    return parents
