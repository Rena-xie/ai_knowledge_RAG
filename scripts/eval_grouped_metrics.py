"""按独立问题和语言重新聚合检索评测指标。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_QUESTION_ID = re.compile(r"(q\d+)[a-z]*$")
_LANGUAGES = frozenset({"zh", "en", "mixed"})


def base_id(question_id: str) -> str:
    """把 q01a 归到 q01；不符合命名规则时返回原 id。"""

    match = _QUESTION_ID.fullmatch(question_id)
    return match.group(1) if match else question_id


def _average(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 4)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _question_language(
    question: dict[str, Any],
    default_language: str,
) -> str:
    language = str(question.get("language", default_language))
    if language not in _LANGUAGES:
        raise ValueError(f"题目语言不合法: {question['id']}={language}")
    return language


def _group_summary(
    group_id: str,
    variants: list[dict[str, Any]],
) -> dict[str, Any]:
    labeled = [variant for variant in variants if variant["labeled"]]
    unlabeled = [variant for variant in variants if not variant["labeled"]]
    return {
        "group_id": group_id,
        "category": variants[0]["category"],
        "languages": sorted({str(variant["language"]) for variant in variants}),
        "variants": len(variants),
        "labeled": len(labeled),
        "recall_at_5": _average(
            [variant["recall_at_5"] for variant in labeled]
        ),
        "precision_at_5": _average(
            [variant["precision_at_5"] for variant in labeled]
        ),
        "hit_rate_at_5": _ratio(
            sum(1 for variant in labeled if variant["hit_at_5"]),
            len(labeled),
        ),
        "mrr_at_10": _average(
            [variant["mrr_at_10"] for variant in labeled]
        ),
        "ndcg_at_10": _average(
            [variant["ndcg_at_10"] for variant in labeled]
        ),
        "empty_evidence_accuracy": _ratio(
            sum(
                1
                for variant in unlabeled
                if variant.get(
                    "empty_evidence", not variant.get("retrieved", [])
                )
            ),
            len(unlabeled),
        ),
    }


def _aggregate_groups(groups: Sequence[dict[str, Any]]) -> dict[str, Any]:
    labeled = [group for group in groups if group["labeled"] > 0]
    unlabeled = [group for group in groups if group["labeled"] == 0]
    return {
        "variants": sum(group["variants"] for group in groups),
        "groups": len(groups),
        "labeled_groups": len(labeled),
        "unlabeled_groups": len(unlabeled),
        "recall_at_5": _average([group["recall_at_5"] for group in labeled]),
        "precision_at_5": _average(
            [group["precision_at_5"] for group in labeled]
        ),
        "hit_rate_at_5": _average(
            [group["hit_rate_at_5"] for group in labeled]
        ),
        "mrr_at_10": _average([group["mrr_at_10"] for group in labeled]),
        "ndcg_at_10": _average([group["ndcg_at_10"] for group in labeled]),
        "empty_evidence_accuracy": _average(
            [group["empty_evidence_accuracy"] for group in unlabeled]
        ),
    }


def _variant_totals(
    grouped: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    variants = [variant for members in grouped.values() for variant in members]
    labeled = [variant for variant in variants if variant["labeled"]]
    unlabeled = [variant for variant in variants if not variant["labeled"]]
    return {
        "variants": len(variants),
        "labeled_variants": len(labeled),
        "recall_at_5": _average(
            [variant["recall_at_5"] for variant in labeled]
        ),
        "precision_at_5": _average(
            [variant["precision_at_5"] for variant in labeled]
        ),
        "hit_rate_at_5": _average(
            [1.0 if variant["hit_at_5"] else 0.0 for variant in labeled]
        ),
        "mrr_at_10": _average(
            [variant["mrr_at_10"] for variant in labeled]
        ),
        "ndcg_at_10": _average(
            [variant["ndcg_at_10"] for variant in labeled]
        ),
        "empty_evidence_accuracy": _ratio(
            sum(
                1
                for variant in unlabeled
                if variant.get(
                    "empty_evidence", not variant.get("retrieved", [])
                )
            ),
            len(unlabeled),
        ),
    }


def _language_metrics(
    grouped: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    for language in sorted(_LANGUAGES):
        language_groups: list[dict[str, Any]] = []
        for group_id, variants in grouped.items():
            selected = [
                variant
                for variant in variants
                if variant["language"] == language
            ]
            if selected:
                language_groups.append(_group_summary(group_id, selected))
        if language_groups:
            metrics[language] = _aggregate_groups(language_groups)
    return metrics


def group_results(
    questions_payload: dict[str, Any],
    results_payload: dict[str, Any],
) -> dict[str, Any]:
    """按独立问题聚合，并额外计算语言切片。"""

    default_language = str(questions_payload.get("default_language", "zh"))
    questions = {str(item["id"]): item for item in questions_payload["questions"]}
    results = {str(item["question_id"]): item for item in results_payload["results"]}
    missing = sorted(set(questions) - set(results))
    extra = sorted(set(results) - set(questions))
    if missing:
        print(
            f"警告：结果文件缺少 {len(missing)} 道题，已跳过："
            f"{', '.join(missing[:10])}",
            file=sys.stderr,
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for question_id, question in questions.items():
        if question_id not in results:
            continue
        grouped[base_id(question_id)].append(
            {
                **results[question_id],
                "question_id": question_id,
                "category": str(question["category"]),
                "language": _question_language(question, default_language),
                "labeled": bool(question.get("relevant_child_ids")),
            }
        )

    groups = [
        _group_summary(group_id, variants)
        for group_id, variants in grouped.items()
    ]
    groups.sort(key=lambda item: int(re.sub(r"\D", "", item["group_id"]) or 0))
    category_metrics = {
        category: _aggregate_groups(
            [group for group in groups if group["category"] == category]
        )
        for category in sorted({str(group["category"]) for group in groups})
    }
    return {
        "questions_version": questions_payload.get("version"),
        "default_language": default_language,
        "corpus": questions_payload.get("corpus"),
        "chunker_version": questions_payload.get("chunker_version"),
        "snapshot": questions_payload.get("snapshot"),
        "group_definition": (
            "题目 id 去掉末尾小写字母后缀后归为一组；"
            "q01/q01a/q01b 视为同一个独立问题。"
        ),
        "totals": _aggregate_groups(groups),
        "variant_weighted_totals": _variant_totals(grouped),
        "language_metrics": _language_metrics(grouped),
        "category_metrics": category_metrics,
        "missing_question_ids": missing,
        "extra_result_ids": extra,
        "groups": groups,
    }


def merge_result_payloads(paths: Sequence[Path]) -> dict[str, Any]:
    """合并互补的原始结果文件，拒绝重复题目。"""

    merged: dict[str, dict[str, Any]] = {}
    rerank_values: set[bool] = set()
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "rerank_enabled" in payload:
            rerank_values.add(bool(payload["rerank_enabled"]))
        for result in payload.get("results", []):
            question_id = str(result["question_id"])
            if question_id in merged:
                raise ValueError(f"结果文件包含重复题目: {question_id}")
            merged[question_id] = result
    if len(rerank_values) > 1:
        raise ValueError("不能合并 rerank 配置不同的结果文件")
    return {
        "results": list(merged.values()),
        "rerank_enabled": next(iter(rerank_values), None),
    }


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def _print_metrics(label: str, metrics: dict[str, Any]) -> None:
    groups = metrics.get("groups", "-")
    variants = metrics.get("variants", "-")
    print(
        f"{label}: groups={groups} variants={variants} "
        f"Recall@5={_fmt(metrics['recall_at_5'])} "
        f"Precision@5={_fmt(metrics['precision_at_5'])} "
        f"HitRate@5={_fmt(metrics['hit_rate_at_5'])} "
        f"MRR@10={_fmt(metrics['mrr_at_10'])} "
        f"nDCG@10={_fmt(metrics['ndcg_at_10'])} "
        f"EmptyEvidence={_fmt(metrics['empty_evidence_accuracy'])}"
    )


def _print_table(payload: dict[str, Any]) -> None:
    _print_metrics("全部独立问题", payload["totals"])
    _print_metrics("按问法加权", payload["variant_weighted_totals"])
    print()
    for language, metrics in payload["language_metrics"].items():
        _print_metrics(f"语言={language}", metrics)
    print()
    for category, metrics in payload["category_metrics"].items():
        _print_metrics(f"类别={category}", metrics)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按独立问题和语言聚合评测指标")
    parser.add_argument(
        "--questions", type=Path, default=Path("tests/evals/questions.json")
    )
    parser.add_argument(
        "--results",
        type=Path,
        nargs="+",
        required=True,
        help="一个或多个互补的原始结果 JSON",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedding-model")
    parser.add_argument("--rerank-model")
    parser.add_argument("--source-commit")
    parser.add_argument("--note")
    args = parser.parse_args(argv)

    questions_payload = json.loads(args.questions.read_text(encoding="utf-8"))
    results_payload = merge_result_payloads(args.results)
    payload = group_results(questions_payload, results_payload)
    payload.update(
        {
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "questions_file": str(args.questions),
            "results_files": [str(path) for path in args.results],
            "rerank_enabled": results_payload["rerank_enabled"],
            "embedding_model": args.embedding_model,
            "rerank_model": args.rerank_model,
            "source_commit": args.source_commit,
            "metric_source": "final_evidences",
            "fused_ids_usage": "diagnostic_only",
            "note": args.note,
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _print_table(payload)
    print(f"\n输出: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
