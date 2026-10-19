"""按独立问题重新聚合评测指标。

同义改写题（q01 / q01a / q01b）共用同一组相关 child，不构成新的独立样本。
本脚本把去掉末尾小写字母后的 id 视为同一问题，按组等权重新计算分类指标，
避免改写题多的类别在汇总结果中被放大。

用法：

    python scripts/eval_grouped_metrics.py \
        --questions tests/evals/questions.json \
        --results tests/evals/results/2026-10-19-v2-108q-final2-standard.json \
        --output tests/evals/results/2026-10-19-v2-108q-final2-grouped.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_QUESTION_ID = re.compile(r"(q\d+)[a-z]*$")


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


def group_results(
    questions_payload: dict[str, Any], results_payload: dict[str, Any]
) -> dict[str, Any]:
    """按独立问题聚合单题结果。"""

    questions = {str(item["id"]): item for item in questions_payload["questions"]}
    results = {str(item["question_id"]): item for item in results_payload["results"]}
    missing = sorted(set(questions) - set(results))
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
                "question_id": question_id,
                "category": str(question["category"]),
                "labeled": bool(question.get("relevant_child_ids")),
                **results[question_id],
            }
        )

    groups: list[dict[str, Any]] = []
    for group_key, variants in grouped.items():
        labeled = [item for item in variants if item["labeled"]]
        unlabeled = [item for item in variants if not item["labeled"]]
        groups.append(
            {
                "group_id": group_key,
                "category": variants[0]["category"],
                "variants": len(variants),
                "labeled": len(labeled),
                "recall_at_5": _average([item["recall_at_5"] for item in labeled]),
                "hit_rate_at_5": (
                    _ratio(sum(1 for item in labeled if item["hit_at_5"]), len(labeled))
                ),
                "mrr_at_10": _average([item["mrr_at_10"] for item in labeled]),
                "ndcg_at_10": _average([item["ndcg_at_10"] for item in labeled]),
                "insufficient_accuracy": _ratio(
                    sum(
                        1
                        for item in unlabeled
                        if item["answer_type"] == "insufficient_evidence"
                    ),
                    len(unlabeled),
                ),
            }
        )

    groups.sort(key=lambda item: int(re.sub(r"\D", "", item["group_id"]) or 0))
    categories = sorted({item["category"] for item in groups})
    category_metrics: dict[str, dict[str, Any]] = {}
    for category in categories:
        members = [item for item in groups if item["category"] == category]
        labeled = [item for item in members if item["labeled"] > 0]
        unlabeled = [item for item in members if item["labeled"] == 0]
        category_metrics[category] = {
            "groups": len(members),
            "variants": sum(item["variants"] for item in members),
            "labeled_groups": len(labeled),
            "recall_at_5": _average([item["recall_at_5"] for item in labeled]),
            "hit_rate_at_5": _average(
                [item["hit_rate_at_5"] for item in labeled]
            ),
            "mrr_at_10": _average([item["mrr_at_10"] for item in labeled]),
            "ndcg_at_10": _average([item["ndcg_at_10"] for item in labeled]),
            "insufficient_accuracy": _average(
                [item["insufficient_accuracy"] for item in unlabeled]
            ),
        }

    labeled_groups = [item for item in groups if item["labeled"] > 0]
    unlabeled_groups = [item for item in groups if item["labeled"] == 0]
    labeled_variants = [
        item
        for variants in grouped.values()
        for item in variants
        if item["labeled"]
    ]
    unlabeled_variants = [
        item
        for variants in grouped.values()
        for item in variants
        if not item["labeled"]
    ]
    variant_totals = {
        "labeled_variants": len(labeled_variants),
        "recall_at_5": _average(
            [item["recall_at_5"] for item in labeled_variants]
        ),
        "hit_rate_at_5": _average(
            [1.0 if item["hit_at_5"] else 0.0 for item in labeled_variants]
        ),
        "mrr_at_10": _average([item["mrr_at_10"] for item in labeled_variants]),
        "ndcg_at_10": _average(
            [item["ndcg_at_10"] for item in labeled_variants]
        ),
        "insufficient_accuracy": _ratio(
            sum(
                1
                for item in unlabeled_variants
                if item["answer_type"] == "insufficient_evidence"
            ),
            len(unlabeled_variants),
        ),
    }
    totals = {
        "variants": sum(item["variants"] for item in groups),
        "groups": len(groups),
        "labeled_groups": len(labeled_groups),
        "unlabeled_groups": len(unlabeled_groups),
        "recall_at_5": _average([item["recall_at_5"] for item in labeled_groups]),
        "hit_rate_at_5": _average(
            [item["hit_rate_at_5"] for item in labeled_groups]
        ),
        "mrr_at_10": _average([item["mrr_at_10"] for item in labeled_groups]),
        "ndcg_at_10": _average([item["ndcg_at_10"] for item in labeled_groups]),
        "insufficient_accuracy": _average(
            [item["insufficient_accuracy"] for item in unlabeled_groups]
        ),
    }
    return {
        "source_results": results_payload.get("source_result"),
        "questions_version": questions_payload.get("version"),
        "corpus": questions_payload.get("corpus"),
        "snapshot": questions_payload.get("snapshot"),
        "group_definition": (
            "题目 id 去掉末尾小写字母后缀后归为一组；"
            "q01/q01a/q01b 视为同一个独立问题。"
        ),
        "totals": totals,
        "variant_weighted_totals": variant_totals,
        "missing_question_ids": missing,
        "category_metrics": category_metrics,
        "groups": groups,
    }


def _print_table(payload: dict[str, Any]) -> None:
    totals = payload["totals"]
    print(
        f"独立问题 {totals['groups']} 个（含改写题 {totals['variants']} 道）；"
        f"标准 Recall@5 = {totals['recall_at_5']}，"
        f"HitRate@5 = {totals['hit_rate_at_5']}"
    )
    variants = payload["variant_weighted_totals"]
    print(
        f"按题加权对照：Recall@5 = {variants['recall_at_5']}，"
        f"HitRate@5 = {variants['hit_rate_at_5']}"
    )
    print()
    print(f"{'类别':<8}{'独立题':>6}{'改写题':>7}{'Recall@5':>11}{'HitRate@5':>11}{'MRR@10':>9}")
    for category, metrics in payload["category_metrics"].items():
        print(
            f"{category:<8}{metrics['groups']:>6}{metrics['variants']:>7}"
            f"{_fmt(metrics['recall_at_5']):>11}"
            f"{_fmt(metrics['hit_rate_at_5']):>11}"
            f"{_fmt(metrics['mrr_at_10']):>9}"
        )


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按独立问题聚合评测指标")
    parser.add_argument(
        "--questions", type=Path, default=Path("tests/evals/questions.json")
    )
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    questions_payload = json.loads(args.questions.read_text(encoding="utf-8"))
    results_payload = json.loads(args.results.read_text(encoding="utf-8"))
    payload = group_results(questions_payload, results_payload)
    payload["questions_file"] = str(args.questions)
    payload["results_file"] = str(args.results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_table(payload)
    print(f"\n输出: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
