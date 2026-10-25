"""正文指纹规范化。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

FINGERPRINT_VERSION = "v1"


@dataclass(frozen=True)
class ContentFingerprint:
    """版本化正文指纹。"""

    value: str
    version: str


def normalize_markdown(markdown: str) -> str:
    """统一不会改变正文语义的格式差异。"""

    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    return normalized.strip()


def compute_content_fingerprint(markdown: str) -> ContentFingerprint:
    """对规范化 Markdown 计算 SHA-256。"""

    normalized = normalize_markdown(markdown)
    value = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return ContentFingerprint(value=value, version=FINGERPRINT_VERSION)
