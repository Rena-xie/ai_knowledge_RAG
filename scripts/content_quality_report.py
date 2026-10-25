"""评估正文提取黄金集并生成逐页与分语言报告。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.domain.language import detect_language  # noqa: E402
from app.infra.parser import ParsedDocument, ParseError, parse_html  # noqa: E402

DEFAULT_CASES_PATH = PROJECT_ROOT / "tests/content_quality/cases.json"
MIN_TITLE_ACCURACY = 0.95
MIN_LANGUAGE_ACCURACY = 0.95
MIN_BODY_RECALL = 0.95
MAX_NOISE_RATE = 0.10
MIN_STRUCTURE_RETENTION = 0.95
MIN_HEADING_RETENTION = 0.95
_WHITESPACE_RE = re.compile(r"\s+")
_FENCED_CODE_RE = re.compile(
    r"(?ms)^```[^\n]*\n(.*?)^```[ \t]*$"
)
_HEADING_RE = re.compile(r"^#{1,6}\s+.*\S", re.MULTILINE)
_LIST_LINE_RE = re.compile(
    r"^\s*(?:[-+*]|\d+\.)\s+.*\S",
    re.MULTILINE,
)
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)


@dataclass(frozen=True)
class CaseMetrics:
    case_id: str
    language: str
    category: str
    title_expected: str
    title_actual: str
    title_correct: bool
    language_expected: str
    language_detected: str
    language_correct: bool
    body_char_recall: float
    noise_rate: float
    structure_retention: float
    heading_retention: float
    output_chars: int
    failed_markers: tuple[str, ...]


@dataclass(frozen=True)
class AggregateMetrics:
    case_count: int
    non_empty_rate: float
    title_accuracy: float
    language_accuracy: float
    body_char_recall: float
    noise_rate: float
    structure_retention: float
    heading_retention: float


@dataclass(frozen=True)
class LanguageReport:
    language: str
    aggregate: AggregateMetrics
    cases: tuple[CaseMetrics, ...]


@dataclass(frozen=True)
class CaseAnnotations:
    body_markers: tuple[str, ...]
    noise_markers: tuple[str, ...]
    structures: tuple[dict[str, Any], ...]
    heading_markers: tuple[str, ...]


@dataclass(frozen=True)
class QualityDataset:
    version: int
    captured_on: str
    cases: tuple[dict[str, Any], ...]


def normalize_text(value: str) -> str:
    """归一化用于标题和正文标记比较的文本。"""

    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.replace("\u00a0", " ")
    return _WHITESPACE_RE.sub(" ", normalized).strip().casefold()


def _normalize_code(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n")
    return normalized.replace("\r", "\n").strip()


def read_snapshot_bytes(case: dict[str, Any]) -> bytes:
    snapshot_path = Path(str(case["snapshot_path"]))
    if not snapshot_path.is_absolute():
        snapshot_path = PROJECT_ROOT / snapshot_path
    if snapshot_path.suffix == ".gz":
        with gzip.open(snapshot_path, "rb") as file:
            return file.read()
    return snapshot_path.read_bytes()


def read_snapshot_text(case: dict[str, Any]) -> str:
    raw = read_snapshot_bytes(case)
    expected_hash = str(case["snapshot_sha256"])
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(
            f"{case['id']} 快照哈希不匹配: {actual_hash} != {expected_hash}"
        )
    return raw.decode("utf-8")


def load_dataset(cases_path: Path = DEFAULT_CASES_PATH) -> QualityDataset:
    document = json.loads(cases_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("cases.json 顶层必须是对象")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not all(
        isinstance(case, dict) for case in raw_cases
    ):
        raise ValueError("cases 必须是对象数组")
    return QualityDataset(
        version=int(document["version"]),
        captured_on=str(document["captured_on"]),
        cases=tuple(raw_cases),
    )


def _marker_is_present(text: str, marker: str) -> bool:
    return normalize_text(marker) in normalize_text(text)


def _marker_char_recall(text: str, markers: list[str]) -> float:
    total_chars = sum(len(normalize_text(marker)) for marker in markers)
    if total_chars == 0:
        return 1.0
    retained_chars = sum(
        len(normalize_text(marker))
        for marker in markers
        if _marker_is_present(text, marker)
    )
    return retained_chars / total_chars


def _noise_rate(text: str, markers: list[str]) -> float:
    normalized = normalize_text(text)
    if not normalized:
        return 1.0
    noise_chars = 0
    for marker in markers:
        normalized_marker = normalize_text(marker)
        if normalized_marker:
            noise_chars += normalized.count(normalized_marker) * len(
                normalized_marker
            )
    return min(noise_chars / len(normalized), 1.0)


def _fenced_code(text: str) -> str:
    return "\n".join(match.group(1) for match in _FENCED_CODE_RE.finditer(text))


def _structure_retained(text: str, structure: dict[str, Any]) -> bool:
    kind = str(structure["type"])
    marker = str(structure["marker"])
    if kind == "code":
        return _normalize_code(marker) in _normalize_code(_fenced_code(text))
    if kind == "list":
        return any(
            _marker_is_present(line, marker)
            for line in _LIST_LINE_RE.findall(text)
        )
    if kind == "table":
        table_text = "\n".join(
            match.group(0) for match in _TABLE_LINE_RE.finditer(text)
        )
        return _marker_is_present(table_text, marker)
    raise ValueError(f"未知结构类型: {kind}")


def _structure_retention(
    text: str, structures: list[dict[str, Any]]
) -> float:
    if not structures:
        return 1.0
    retained = sum(
        1 for structure in structures if _structure_retained(text, structure)
    )
    return retained / len(structures)


def _heading_retention(text: str, markers: list[str]) -> float:
    if not markers:
        return 1.0
    headings = "\n".join(_HEADING_RE.findall(text))
    return _marker_char_recall(headings, markers)


def _failed_case_metrics(
    case: dict[str, Any],
    message: str,
) -> CaseMetrics:
    return CaseMetrics(
        case_id=str(case["id"]),
        language=str(case["language"]),
        category=str(case["category"]),
        title_expected=str(case["expected_title"]),
        title_actual="",
        title_correct=False,
        language_expected=str(case["language"]),
        language_detected="unknown",
        language_correct=False,
        body_char_recall=0.0,
        noise_rate=1.0,
        structure_retention=0.0,
        heading_retention=0.0,
        output_chars=0,
        failed_markers=(message,),
    )


def _case_annotations(case: dict[str, Any]) -> CaseAnnotations:
    return CaseAnnotations(
        body_markers=tuple(str(marker) for marker in case["body_markers"]),
        noise_markers=tuple(str(marker) for marker in case["noise_markers"]),
        structures=tuple(
            structure
            for structure in case["structure_markers"]
            if isinstance(structure, dict)
        ),
        heading_markers=tuple(
            str(marker) for marker in case["heading_markers"]
        ),
    )


def _metrics_from_document(
    case: dict[str, Any],
    document: ParsedDocument,
) -> CaseMetrics:
    title = document.title or ""
    markdown = document.markdown
    expected_title = str(case["expected_title"])
    annotations = _case_annotations(case)
    detected_language = detect_language(markdown)
    failed_markers = tuple(
        marker
        for marker in annotations.body_markers
        if not _marker_is_present(markdown, marker)
    )
    return CaseMetrics(
        case_id=str(case["id"]),
        language=str(case["language"]),
        category=str(case["category"]),
        title_expected=expected_title,
        title_actual=title,
        title_correct=normalize_text(title) == normalize_text(expected_title),
        language_expected=str(case["language"]),
        language_detected=detected_language,
        language_correct=detected_language == str(case["language"]),
        body_char_recall=_marker_char_recall(
            markdown, list(annotations.body_markers)
        ),
        noise_rate=_noise_rate(markdown, list(annotations.noise_markers)),
        structure_retention=_structure_retention(
            markdown, list(annotations.structures)
        ),
        heading_retention=_heading_retention(
            markdown, list(annotations.heading_markers)
        ),
        output_chars=len(markdown),
        failed_markers=failed_markers,
    )


def evaluate_html(case: dict[str, Any], html: str) -> CaseMetrics:
    """评估给定 HTML；供冻结快照与显式 live 检查共用。"""

    try:
        document = parse_html(html, url=str(case.get("final_url") or ""))
    except ParseError as error:
        return _failed_case_metrics(case, error.message)
    return _metrics_from_document(case, document)


def evaluate_case(case: dict[str, Any]) -> CaseMetrics:
    return evaluate_html(case, read_snapshot_text(case))


def aggregate_metrics(cases: list[CaseMetrics]) -> AggregateMetrics:
    if not cases:
        return AggregateMetrics(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    count = len(cases)
    total_output_chars = sum(case.output_chars for case in cases)
    return AggregateMetrics(
        case_count=count,
        non_empty_rate=sum(case.output_chars > 0 for case in cases) / count,
        title_accuracy=sum(case.title_correct for case in cases) / count,
        language_accuracy=sum(case.language_correct for case in cases) / count,
        body_char_recall=sum(case.body_char_recall for case in cases) / count,
        noise_rate=(
            sum(case.noise_rate * case.output_chars for case in cases)
            / total_output_chars
            if total_output_chars
            else 1.0
        ),
        structure_retention=(
            sum(case.structure_retention for case in cases) / count
        ),
        heading_retention=(
            sum(case.heading_retention for case in cases) / count
        ),
    )


def _group_language_reports(
    case_metrics: list[CaseMetrics],
) -> list[LanguageReport]:
    languages = sorted({case.language for case in case_metrics})
    return [
        LanguageReport(
            language=language,
            aggregate=aggregate_metrics(
                [case for case in case_metrics if case.language == language]
            ),
            cases=tuple(
                case for case in case_metrics if case.language == language
            ),
        )
        for language in languages
    ]


def build_report(cases_path: Path = DEFAULT_CASES_PATH) -> dict[str, Any]:
    dataset = load_dataset(cases_path)
    case_metrics = [evaluate_case(case) for case in dataset.cases]
    language_reports = _group_language_reports(case_metrics)
    return {
        "dataset_version": dataset.version,
        "captured_on": dataset.captured_on,
        "overall": asdict(aggregate_metrics(case_metrics)),
        "languages": [
            {
                "language": language_report.language,
                "aggregate": asdict(language_report.aggregate),
                "cases": [
                    asdict(case) for case in language_report.cases
                ],
            }
            for language_report in language_reports
        ],
    }


def dump_documents(
    output_dir: Path,
    cases_path: Path = DEFAULT_CASES_PATH,
) -> None:
    """写出提取后的 Markdown，供人工标注和失败样本审查。"""

    dataset = load_dataset(cases_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    for case in dataset.cases:
        case_id = str(case["id"])
        document = parse_html(
            read_snapshot_text(case),
            url=str(case.get("final_url") or ""),
        )
        (output_dir / f"{case_id}.md").write_text(
            document.markdown,
            encoding="utf-8",
        )


def _format_percent(value: float) -> str:
    return f"{value:.2%}"


def _print_aggregate(label: str, aggregate: AggregateMetrics) -> None:
    print(
        f"{label}: cases={aggregate.case_count} "
        f"non_empty={_format_percent(aggregate.non_empty_rate)} "
        f"title={_format_percent(aggregate.title_accuracy)} "
        f"language={_format_percent(aggregate.language_accuracy)} "
        f"body={_format_percent(aggregate.body_char_recall)} "
        f"noise={_format_percent(aggregate.noise_rate)} "
        f"structure={_format_percent(aggregate.structure_retention)} "
        f"heading={_format_percent(aggregate.heading_retention)}"
    )


def _case_needs_attention(case: CaseMetrics) -> bool:
    return (
        not case.title_correct
        or not case.language_correct
        or case.body_char_recall < 1.0
        or case.noise_rate > 0.0
        or case.structure_retention < 1.0
        or case.heading_retention < 1.0
    )


def _print_case_failure(case: CaseMetrics) -> None:
    print(
        f"  FAIL {case.case_id}: "
        f"title={case.title_correct} "
        f"language={case.language_detected} "
        f"body={_format_percent(case.body_char_recall)} "
        f"noise={_format_percent(case.noise_rate)} "
        f"structure={_format_percent(case.structure_retention)} "
        f"heading={_format_percent(case.heading_retention)} "
        f"missing={case.failed_markers}"
    )


def _print_language_reports(report: dict[str, Any]) -> None:
    for language in report["languages"]:
        aggregate = AggregateMetrics(**language["aggregate"])
        _print_aggregate(language["language"], aggregate)
        for raw_case in language["cases"]:
            case = CaseMetrics(**raw_case)
            if _case_needs_attention(case):
                _print_case_failure(case)


def _quality_passed(overall: AggregateMetrics) -> bool:
    return (
        overall.non_empty_rate >= 1.0
        and overall.title_accuracy >= MIN_TITLE_ACCURACY
        and overall.language_accuracy >= MIN_LANGUAGE_ACCURACY
        and overall.body_char_recall >= MIN_BODY_RECALL
        and overall.noise_rate <= MAX_NOISE_RATE
        and overall.structure_retention >= MIN_STRUCTURE_RETENTION
        and overall.heading_retention >= MIN_HEADING_RETENTION
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成正文提取质量报告")
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help="黄金集 cases.json",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="将完整报告写入指定 JSON 文件",
    )
    parser.add_argument(
        "--dump-markdown",
        type=Path,
        help="将逐页提取结果写入目录，供人工审查",
    )
    args = parser.parse_args()

    quality_report = build_report(args.cases)
    if args.dump_markdown is not None:
        dump_documents(args.dump_markdown, args.cases)
    _print_language_reports(quality_report)
    overall = AggregateMetrics(**quality_report["overall"])
    _print_aggregate("overall", overall)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(quality_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0 if _quality_passed(overall) else 1


if __name__ == "__main__":
    raise SystemExit(main())
