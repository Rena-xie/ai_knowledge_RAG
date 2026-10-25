"""canonical 与 AMP 链接线索。"""

from app.domain.source_links import parse_related_document_links


def test_extracts_relative_same_host_links() -> None:
    html = """
    <html><head>
      <link rel="canonical" href="/article">
      <link rel="amphtml alternate" href="/article/amp">
    </head></html>
    """

    links = parse_related_document_links(
        html, final_url="https://example.com/article?utm_source=feed"
    )

    assert links.declared_canonical == "https://example.com/article"
    assert links.amp_html == "https://example.com/article/amp"


def test_ignores_cross_host_and_non_http_links() -> None:
    html = """
    <link rel="canonical" href="https://other.example/article">
    <link rel="amphtml" href="javascript:alert(1)">
    """

    links = parse_related_document_links(
        html, final_url="https://example.com/article"
    )

    assert links.declared_canonical is None
    assert links.amp_html is None


def test_uses_first_matching_declaration() -> None:
    html = """
    <link rel="canonical" href="/first">
    <link rel="canonical" href="/second">
    """

    links = parse_related_document_links(
        html, final_url="https://example.com/article"
    )

    assert links.declared_canonical == "https://example.com/first"
