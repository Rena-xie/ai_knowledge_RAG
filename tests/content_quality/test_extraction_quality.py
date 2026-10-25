"""真实网页快照的正文质量与语言回归门禁。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.domain.language import detect_language
from app.infra.parser import parse_html
from scripts.content_quality_report import (
    DEFAULT_CASES_PATH,
    MAX_NOISE_RATE,
    MIN_BODY_RECALL,
    MIN_HEADING_RETENTION,
    MIN_LANGUAGE_ACCURACY,
    MIN_STRUCTURE_RETENTION,
    MIN_TITLE_ACCURACY,
    PROJECT_ROOT,
    build_report,
    read_snapshot_bytes,
    read_snapshot_text,
)

CONTENT_QUALITY_DIR = PROJECT_ROOT / "tests/content_quality"
BASELINE_PATH = CONTENT_QUALITY_DIR / "baseline.json"
_WRAPPER_TAG_RE = re.compile(
    r"</?(?:span|font|sup|sub|small|mark|kbd|abbr)\b",
    re.IGNORECASE,
)


def _load_cases() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = json.loads(DEFAULT_CASES_PATH.read_text(encoding="utf-8"))
    cases = data.get("cases")
    assert isinstance(cases, list)
    return data, [case for case in cases if isinstance(case, dict)]


def test_dataset_contract_and_snapshot_integrity() -> None:
    data, cases = _load_cases()
    public_cases = [case for case in cases if case["origin"] == "public_page"]
    mixed_cases = [case for case in cases if case["language"] == "mixed"]

    assert data["version"] == 1
    assert len(public_cases) >= 30
    assert sum(case["language"] == "zh" for case in public_cases) >= 15
    assert sum(case["language"] == "en" for case in public_cases) >= 15
    assert len(mixed_cases) >= 2
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case["source_url"] for case in cases}) == len(cases)

    structure_types = {
        structure["type"]
        for case in cases
        for structure in case["structure_markers"]
    }
    assert {"code", "list", "table"} <= structure_types

    for case in cases:
        raw = read_snapshot_bytes(case)
        html = read_snapshot_text(case)
        assert hashlib.sha256(raw).hexdigest() == case["snapshot_sha256"]
        assert case["expected_title"]
        assert len(case["body_markers"]) >= 2
        assert case["heading_markers"]
        assert case["noise_markers"]
        assert all(
            marker.casefold() in html.casefold()
            for marker in case["noise_markers"]
        )
        if case["origin"] == "public_page":
            assert case["source_url"].startswith("https://")
            assert case["final_url"].startswith("https://")
            assert case["http_status"] == 200
            assert "html" in case["content_type"].casefold()
            assert case["captured_on"]


def test_all_public_pages_extract_clean_non_empty_markdown() -> None:
    _, cases = _load_cases()
    public_cases = [case for case in cases if case["origin"] == "public_page"]

    for case in public_cases:
        document = parse_html(
            read_snapshot_text(case),
            url=case["final_url"],
        )
        assert document.markdown
        assert document.title
        assert "¶" not in document.markdown
        assert not _WRAPPER_TAG_RE.search(document.markdown)
        assert document.markdown.count("```") % 2 == 0


def test_dataset_language_labels_match_deterministic_detector() -> None:
    _, cases = _load_cases()

    for case in cases:
        document = parse_html(
            read_snapshot_text(case),
            url=str(case.get("final_url") or ""),
        )
        assert detect_language(document.markdown) == case["language"]


def test_content_quality_thresholds_pass_for_each_language() -> None:
    report = build_report(DEFAULT_CASES_PATH)
    aggregates = [
        report["overall"],
        *(item["aggregate"] for item in report["languages"]),
    ]

    for aggregate in aggregates:
        assert aggregate["non_empty_rate"] >= 1.0
        assert aggregate["title_accuracy"] >= MIN_TITLE_ACCURACY
        assert aggregate["language_accuracy"] >= MIN_LANGUAGE_ACCURACY
        assert aggregate["body_char_recall"] >= MIN_BODY_RECALL
        assert aggregate["noise_rate"] <= MAX_NOISE_RATE
        assert aggregate["structure_retention"] >= MIN_STRUCTURE_RETENTION
        assert aggregate["heading_retention"] >= MIN_HEADING_RETENTION


def test_frozen_baseline_preserves_parser_improvement_evidence() -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    current = build_report(DEFAULT_CASES_PATH)

    assert baseline["dataset_version"] == 1
    assert baseline["overall"]["title_accuracy"] < MIN_TITLE_ACCURACY
    assert (
        current["overall"]["title_accuracy"]
        > baseline["overall"]["title_accuracy"]
    )
    assert current["overall"]["title_accuracy"] >= MIN_TITLE_ACCURACY
