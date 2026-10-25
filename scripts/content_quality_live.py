"""显式检查当前公开页面，不进入默认 CI。"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.content_quality_report import (  # noqa: E402
    DEFAULT_CASES_PATH,
    MAX_NOISE_RATE,
    MIN_BODY_RECALL,
    MIN_HEADING_RETENTION,
    MIN_STRUCTURE_RETENTION,
    CaseMetrics,
    evaluate_html,
    load_dataset,
)

USER_AGENT = "ai-knowledge-rag/2.0 content-quality live check"
REQUEST_INTERVAL_S = 0.5


@dataclass(frozen=True)
class LiveResult:
    case_id: str
    reachable: bool
    page_changed: bool
    metrics: CaseMetrics | None
    error: str | None = None

    @property
    def meets_thresholds(self) -> bool:
        if self.metrics is None:
            return False
        metrics = self.metrics
        return (
            metrics.output_chars > 0
            and metrics.title_correct
            and metrics.language_correct
            and metrics.body_char_recall >= MIN_BODY_RECALL
            and metrics.noise_rate <= MAX_NOISE_RATE
            and metrics.structure_retention >= MIN_STRUCTURE_RETENTION
            and metrics.heading_retention >= MIN_HEADING_RETENTION
        )


def _public_cases(path: Path) -> list[dict[str, Any]]:
    return [
        case
        for case in load_dataset(path).cases
        if case.get("origin") == "public_page"
    ]


def run_live_check(
    cases_path: Path = DEFAULT_CASES_PATH,
    *,
    interval_s: float = REQUEST_INTERVAL_S,
) -> list[LiveResult]:
    live_results: list[LiveResult] = []
    with httpx.Client(
        follow_redirects=True,
        timeout=30.0,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for case in _public_cases(cases_path):
            case_id = str(case["id"])
            try:
                response = client.get(str(case["source_url"]))
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "html" not in content_type.casefold():
                    raise ValueError(f"非 HTML 响应: {content_type}")
                body = response.content
                metrics = evaluate_html(case, response.text)
                live_results.append(
                    LiveResult(
                        case_id=case_id,
                        reachable=True,
                        page_changed=(
                            hashlib.sha256(body).hexdigest()
                            != str(case["snapshot_sha256"])
                        ),
                        metrics=metrics,
                    )
                )
            except (httpx.HTTPError, ValueError) as error:
                live_results.append(
                    LiveResult(
                        case_id=case_id,
                        reachable=False,
                        page_changed=False,
                        metrics=None,
                        error=str(error),
                    )
                )
            time.sleep(interval_s)
    return live_results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="显式访问当前公开页面并比较冻结正文质量快照"
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help="黄金集 cases.json",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="任一当前页面未达到质量门槛时返回失败",
    )
    args = parser.parse_args()

    live_results = run_live_check(args.cases)
    for result in live_results:
        if result.metrics is None:
            print(f"FAIL {result.case_id}: {result.error}")
            continue
        metrics = result.metrics
        print(
            f"{'PASS' if result.meets_thresholds else 'WARN'} "
            f"{result.case_id} changed={result.page_changed} "
            f"title={metrics.title_correct} "
            f"language={metrics.language_detected} "
            f"body={metrics.body_char_recall:.2%} "
            f"noise={metrics.noise_rate:.2%} "
            f"structure={metrics.structure_retention:.2%} "
            f"heading={metrics.heading_retention:.2%}"
        )

    unreachable = [
        result for result in live_results if not result.reachable
    ]
    below_threshold = [
        result
        for result in live_results
        if result.reachable and not result.meets_thresholds
    ]
    changed = sum(result.page_changed for result in live_results)
    print(
        f"summary total={len(live_results)} changed={changed} "
        f"unreachable={len(unreachable)} "
        f"below_threshold={len(below_threshold)}"
    )
    if unreachable:
        return 1
    if args.strict and below_threshold:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
