"""评测命令：在夹具语料或当前知识库上运行题库（见《项目介绍.md》11.4）。"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
from collections.abc import Sequence
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import select

from app.api.deps import Container
from app.core.clock import utc_now_iso
from app.core.settings import get_settings
from app.infra.db import ChildChunk, KnowledgeSource
from app.infra.fetcher import Fetcher
from app.services.evaluation import EvaluationRunner, load_questions

DEFAULT_CORPUS_DIR = Path("tests/evals/corpus")
DEFAULT_QUESTIONS = Path("tests/evals/questions.json")
DEFAULT_OUTPUT_DIR = Path("tests/evals/results")


class QuietHandler(SimpleHTTPRequestHandler):
    """静默的静态文件处理器。"""

    def log_message(self, format: str, *args: object) -> None:
        return None


def serve_directory(directory: Path) -> tuple[ThreadingHTTPServer, str]:
    """在回环地址上临时提供静态文件，返回服务器与基址。"""

    handler = partial(QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def build_corpus_container(
    corpus_dir: Path, workdir: Path
) -> tuple[Container, ThreadingHTTPServer]:
    """把夹具语料导入临时知识库，返回容器与本地文件服务器。"""

    settings = get_settings().model_copy(
        update={"sqlite_path": workdir / "app.db", "chroma_path": workdir / "chroma"}
    )
    server, base_url = serve_directory(corpus_dir)
    fetcher = Fetcher(
        timeout_s=10,
        max_bytes=settings.fetch_max_bytes,
        max_redirects=settings.fetch_max_redirects,
        max_retries=settings.fetch_max_retries,
        polite_delay_s=0,  # 本地夹具站点：无需限速，避免拖慢评测
        validator=lambda _address: True,  # 本地夹具语料需要放行回环地址
    )
    container = Container(settings, fetcher=fetcher)
    for path in sorted(corpus_dir.glob("*.html")):
        container.ingestion_service.ingest(f"{base_url}/{path.name}")
    return container, server


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
            "source": row[1].rsplit("/", 1)[-1],
            "heading_path": row[2],
            "text": row[3][:120],
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
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR)
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

    temp_context: tempfile.TemporaryDirectory | None = None
    server: ThreadingHTTPServer | None = None
    container: Container | None = None
    try:
        if runner is None:
            if args.use_current_db:
                container = Container(get_settings())
            else:
                temp_context = tempfile.TemporaryDirectory(prefix="aikb-eval-")
                container, server = build_corpus_container(
                    args.corpus_dir, Path(temp_context.name)
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
            "corpus": "current-db" if args.use_current_db else str(args.corpus_dir),
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
        if server is not None:
            server.shutdown()
            server.server_close()
        if temp_context is not None:
            temp_context.cleanup()
