"""提取声明式来源身份链接。"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from app.domain.url import InvalidUrlError, normalize_url

ALLOWED_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class RelatedDocumentLinks:
    """页面声明的 canonical 与 AMP 链接。"""

    declared_canonical: str | None
    amp_html: str | None


class _RelatedLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.declared_canonical: str | None = None
        self.amp_html: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "link":
            return
        values = {name.lower(): value for name, value in attrs}
        relationships = {
            value.lower()
            for value in (values.get("rel") or "").split()
        }
        href = values.get("href")
        if not href:
            return
        if "canonical" in relationships and self.declared_canonical is None:
            self.declared_canonical = href
        if "amphtml" in relationships and self.amp_html is None:
            self.amp_html = href


def _same_host(candidate: str, final_url: str) -> bool:
    candidate_parts = urlsplit(candidate)
    final_parts = urlsplit(final_url)
    if candidate_parts.hostname is None or final_parts.hostname is None:
        return False
    return (
        candidate_parts.hostname.casefold()
        == final_parts.hostname.casefold()
    )


def _normalize_candidate(candidate: str | None, final_url: str) -> str | None:
    if not candidate:
        return None
    absolute = urljoin(final_url, candidate.strip())
    if urlsplit(absolute).scheme.lower() not in ALLOWED_SCHEMES:
        return None
    if not _same_host(absolute, final_url):
        return None
    try:
        return normalize_url(absolute)
    except InvalidUrlError:
        return None


def parse_related_document_links(
    html: str, *, final_url: str
) -> RelatedDocumentLinks:
    """提取同主机 HTTP/HTTPS canonical 与 AMP 链接，忽略其他声明。"""

    parser = _RelatedLinkParser()
    parser.feed(html)
    return RelatedDocumentLinks(
        declared_canonical=_normalize_candidate(
            parser.declared_canonical, final_url
        ),
        amp_html=_normalize_candidate(parser.amp_html, final_url),
    )
