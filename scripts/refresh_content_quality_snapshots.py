"""刷新正文质量黄金集的公开页面快照。

该命令显式访问公开网页，仅用于人工更新冻结快照，不进入 CI。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

import httpx

CASES_PATH = Path("tests/content_quality/cases.json")
SNAPSHOT_DIR = Path("tests/content_quality/snapshots")
USER_AGENT = "ai-knowledge-rag/2.0 content-quality snapshot refresh"
REQUEST_INTERVAL_S = 1.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class RefreshSummary(TypedDict):
    refreshed: int
    skipped: int


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        document = json.load(file)
    if not isinstance(document, dict):
        raise ValueError(f"{path} 顶层必须是对象")
    return document


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_snapshot(path: Path, body: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw_file, gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=raw_file,
        mtime=0,
    ) as compressed:
        compressed.write(body)
    return hashlib.sha256(body).hexdigest()


def refresh_snapshots(
    *,
    cases_path: Path = CASES_PATH,
    snapshot_dir: Path = SNAPSHOT_DIR,
    interval_s: float = REQUEST_INTERVAL_S,
) -> RefreshSummary:
    case_document = _read_json(cases_path)
    case_entries = case_document.get("cases")
    if not isinstance(case_entries, list):
        raise ValueError("cases 必须是数组")

    refreshed = 0
    skipped = 0
    captured_on = datetime.now(UTC).date().isoformat()
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(
        follow_redirects=True,
        timeout=30.0,
        headers=headers,
    ) as client:
        for case in case_entries:
            if not isinstance(case, dict):
                raise ValueError("cases 条目必须是对象")
            if case.get("origin") != "public_page":
                skipped += 1
                continue
            case_id = str(case["id"])
            source_url = str(case["source_url"])
            response = client.get(source_url)
            response.raise_for_status()
            body = response.content
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"{case_id} 页面超过 {MAX_RESPONSE_BYTES} 字节")
            snapshot_name = f"{case_id}.html.gz"
            snapshot_path = snapshot_dir / snapshot_name
            digest = _write_snapshot(snapshot_path, body)
            case.update(
                {
                    "final_url": str(response.url),
                    "http_status": response.status_code,
                    "content_type": response.headers.get(
                        "content-type", "application/octet-stream"
                    ),
                    "snapshot_path": snapshot_path.as_posix(),
                    "snapshot_sha256": digest,
                    "captured_on": captured_on,
                }
            )
            refreshed += 1
            time.sleep(interval_s)

    case_document["captured_on"] = captured_on
    _write_json(cases_path, case_document)
    return {"refreshed": refreshed, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="显式刷新正文质量黄金集的公开网页快照"
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=CASES_PATH,
        help="cases.json 路径",
    )
    parser.add_argument(
        "--snapshots",
        type=Path,
        default=SNAPSHOT_DIR,
        help="快照目录",
    )
    args = parser.parse_args()
    summary = refresh_snapshots(
        cases_path=args.cases,
        snapshot_dir=args.snapshots,
    )
    print(
        f"refreshed={summary['refreshed']} skipped={summary['skipped']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
