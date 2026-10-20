"""评测命令：在真实语料或当前知识库上运行题库（见《项目开发说明_v1.md》11.4）。"""

from __future__ import annotations

import argparse
import base64
import json
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import quote

import httpx
from sqlalchemy import select

from app.api.deps import Container
from app.core.clock import utc_now_iso
from app.core.errors import AppError
from app.core.settings import get_settings
from app.infra.db import ChildChunk, KnowledgeSource
from app.services.evaluation import EvaluationRunner, load_questions

DEFAULT_CORPUS = Path("tests/evals/corpus.json")
DEFAULT_QUESTIONS = Path("tests/evals/questions.json")
DEFAULT_OUTPUT_DIR = Path("tests/evals/results")
DEFAULT_WORKDIR_NAME = "eval-kb"

_GITHUB_BLOB = re.compile(
    r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)"
)


def default_workdir() -> Path:
    """评测库默认位置：与 CHROMA_PATH 同级，保证路径为纯 ASCII（见 10.6）。"""

    return get_settings().chroma_path.parent / DEFAULT_WORKDIR_NAME


def load_corpus(path: Path) -> list[str]:
    """读取语料清单并展开为 URL 列表，保持文件中的顺序。

    入库顺序决定 `child_id`，题库标注依赖该顺序，因此不得重排。
    """

    payload = json.loads(path.read_text(encoding="utf-8"))
    urls = [
        str(url)
        for collection in payload.get("collections", [])
        for url in collection.get("urls", [])
    ]
    if not urls:
        raise ValueError(f"语料清单为空: {path}")
    return urls


def _github_blob_parts(url: str) -> tuple[str, str, str, str] | None:
    match = _GITHUB_BLOB.fullmatch(url)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3), match.group(4)


def fetch_github_markdown(
    client: httpx.Client, url: str
) -> tuple[str, str | None]:
    """通过 GitHub API 读取官方仓库 Markdown，绕开 HTML 页面抓取。”
    """

    parts = _github_blob_parts(url)
    if parts is None:
        raise ValueError(f"不是 GitHub blob 地址: {url}")
    owner, repo, branch, path = parts
    response = client.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/"
        f"{quote(path, safe='/')}",
        params={"ref": branch},
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-knowledge-rag-eval",
        },
    )
    response.raise_for_status()
    payload = response.json()
    encoded = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(encoded, str):
        raise ValueError(f"GitHub API 未返回 content: {url}")
    markdown = base64.b64decode(encoded).decode("utf-8")
    title = next(
        (
            line[2:].strip()
            for line in markdown.splitlines()
            if line.startswith("# ")
        ),
        None,
    )
    return markdown, title


def build_corpus_container(urls: Sequence[str], workdir: Path) -> Container:
    """把语料 URL 依次入库到评测知识库，返回容器。

    已完成的来源会被跳过，因此中断后重跑即可续传；失败的 URL 记录后继续处理，
    结束后打印统计——失败页会改变 `child_id` 分布，标注题库前必须先修掉。
    """

    workdir.mkdir(parents=True, exist_ok=True)
    settings = get_settings().model_copy(
        update={"sqlite_path": workdir / "app.db", "chroma_path": workdir / "chroma"}
    )
    container = Container(settings)
    total = len(urls)
    failed: list[tuple[str, str]] = []
    with httpx.Client(timeout=30, follow_redirects=True) as github_client:
        for index, url in enumerate(urls, start=1):
            try:
                if _github_blob_parts(url) is not None:
                    markdown, title = fetch_github_markdown(
                        github_client, url
                    )
                    result = container.ingestion_service.ingest_markdown(
                        url, markdown, title=title
                    )
                else:
                    result = container.ingestion_service.ingest(url)
            except AppError as error:
                failed.append((url, error.code.value))
                print(f"[{index}/{total}] FAILED {error.code.value} {url}")
                continue
            except Exception as error:  # noqa: BLE001 - 单个页面失败继续
                failed.append((url, type(error).__name__))
                print(
                    f"[{index}/{total}] FAILED "
                    f"{type(error).__name__}: {error} {url}"
                )
                continue
            print(
                f"[{index}/{total}] {result.status} "
                f"triggered={result.triggered} "
                f"children={result.child_count} {url}"
            )
    print(f"入库完成：成功 {total - len(failed)}，失败 {len(failed)}")
    for url, code in failed:
        print(f"  失败 {code} {url}")
    return container


def dump_children(container: Container, target: Path) -> int:
    """导出切块清单，供人工标注相关 child_id。"""

    with container.session_factory() as session:
        rows = session.execute(
            select(
                ChildChunk.id,
                KnowledgeSource.url,
                ChildChunk.heading_path,
                ChildChunk.text,
            )
            .join(KnowledgeSource, ChildChunk.source_id == KnowledgeSource.id)
            .order_by(ChildChunk.id)
        ).all()
    payload = [
        {
            "child_id": row[0],
            "source": row[1],
            "heading_path": row[2],
            "text": row[3][:200],
        }
        for row in rows
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {"children": len(payload), "output": str(target)}, ensure_ascii=False
        )
    )
    return 0


def run_eval(
    argv: Sequence[str] | None = None, *, runner: EvaluationRunner | None = None
) -> int:
    """执行评测并写出结果 JSON。"""

    parser = argparse.ArgumentParser(
        prog="run_eval", description="运行检索与问答评测"
    )
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="评测知识库目录；默认取 CHROMA_PATH 同级的 eval-kb，已完成的来源会跳过",
    )
    parser.add_argument("--output", type=Path, default=None, help="结果输出路径")
    parser.add_argument(
        "--use-current-db", action="store_true", help="使用当前配置的知识库"
    )
    parser.add_argument(
        "--dump-children",
        type=Path,
        default=None,
        help="导出切块清单后退出（用于人工标注）",
    )
    args = parser.parse_args(argv)

    container: Container | None = None
    try:
        if runner is None:
            if args.use_current_db:
                container = Container(get_settings())
            else:
                container = build_corpus_container(
                    load_corpus(args.corpus),
                    args.workdir if args.workdir is not None else default_workdir(),
                )
        if args.dump_children is not None:
            if container is None:
                raise SystemExit("--dump-children 需要真实知识库，不能注入 runner")
            return dump_children(container, args.dump_children)

        questions = load_questions(args.questions)
        active_runner = runner
        if active_runner is None:
            assert container is not None
            active_runner = EvaluationRunner(
                retrieval_service=container.retrieval_service,
                qa_service=container.qa_service,
            )
        report = active_runner.run(questions)
        payload = {
            "created_at": utc_now_iso(),
            "questions_file": str(args.questions),
            "corpus": "current-db" if args.use_current_db else str(args.corpus),
            "workdir": (
                "current-db"
                if args.use_current_db
                else str(
                    args.workdir
                    if args.workdir is not None
                    else default_workdir()
                )
            ),
            "rerank_enabled": get_settings().rerank_enabled,
            "metric_definition": {
                "recall_at_5": "命中的相关 child 数 / 该题全部相关 child 数",
                "hit_rate_at_5": "前 5 条是否至少命中一个相关 child",
                "precision_at_5": "命中的相关 child 数 / 5",
            },
            **report.as_dict(),
        }
        output = args.output or DEFAULT_OUTPUT_DIR / "latest.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        summary = {
            key: payload[key]
            for key in (
                "total",
                "recall_at_5",
                "hit_rate_at_5",
                "precision_at_5",
                "mrr_at_10",
                "ndcg_at_10",
                "citation_validity",
                "insufficient_accuracy",
            )
        }
        summary["output"] = str(output)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    finally:
        if container is not None:
            container.close()
