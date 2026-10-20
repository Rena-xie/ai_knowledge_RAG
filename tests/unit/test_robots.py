"""robots.txt 解析与路径判定（见《项目开发说明_v1.md》9.4）。"""

from app.infra.robots import RobotsPolicy

UA = "ai-knowledge-rag/2.0.0 (+https://github.com/Rena-xie/ai_knowledge_RAG)"


def test_disallow_matches_path_prefix() -> None:
    policy = RobotsPolicy.parse("User-agent: *\nDisallow: /private", UA)

    assert policy.allows("/public/page")
    assert policy.allows("/")
    assert not policy.allows("/private")
    assert not policy.allows("/private/page")


def test_allow_wins_when_patterns_are_equally_specific() -> None:
    policy = RobotsPolicy.parse("User-agent: *\nDisallow: /docs\nAllow: /docs", UA)

    assert policy.allows("/docs")


def test_longest_match_wins() -> None:
    policy = RobotsPolicy.parse(
        "User-agent: *\nDisallow: /docs\nAllow: /docs/public", UA
    )

    assert not policy.allows("/docs/internal")
    assert policy.allows("/docs/public/page")


def test_wildcard_and_end_anchor() -> None:
    policy = RobotsPolicy.parse(
        "User-agent: *\nDisallow: /*.pdf$\nDisallow: /tmp/*/secret", UA
    )

    assert not policy.allows("/files/manual.pdf")
    assert policy.allows("/files/manual.pdf.html")
    assert not policy.allows("/tmp/a/secret")


def test_specific_user_agent_group_wins_over_wildcard() -> None:
    policy = RobotsPolicy.parse(
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: ai-knowledge-rag\nDisallow: /admin\n",
        UA,
    )

    assert policy.allows("/guide")
    assert not policy.allows("/admin")


def test_unrelated_group_falls_back_to_allow_all() -> None:
    policy = RobotsPolicy.parse("User-agent: other-bot\nDisallow: /\n", UA)

    assert policy.allows("/guide")


def test_empty_disallow_allows_everything() -> None:
    policy = RobotsPolicy.parse("User-agent: *\nDisallow:\n", UA)

    assert policy.allows("/anything")


def test_comments_and_unknown_fields_are_ignored() -> None:
    policy = RobotsPolicy.parse(
        "# 注释行\n"
        "User-agent: *  # 行内注释\n"
        "Crawl-delay: 10\n"
        "Sitemap: https://example.test/sitemap.xml\n"
        "Disallow: /secret\n",
        UA,
    )

    assert not policy.allows("/secret")
    assert policy.allows("/other")


def test_empty_document_allows_everything() -> None:
    assert RobotsPolicy.parse("", UA).allows("/anything")
