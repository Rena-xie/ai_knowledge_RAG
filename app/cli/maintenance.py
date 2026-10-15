"""运维命令：重建索引、一致性校验与备份（见 5.4、5.6）。"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from app.core.settings import Settings, get_settings
from app.infra.db import create_db_engine, create_session_factory, init_db
from app.infra.embedding import EmbeddingClient
from app.infra.fts import FtsIndex
from app.infra.vector import VectorIndex
from app.services.maintenance import MaintenanceService


def build_maintenance_service(settings: Settings) -> MaintenanceService:
    """按配置装配维护服务（供脚本使用）。"""

    engine = create_db_engine(settings.sqlite_path)
    init_db(engine)
    fts_index = FtsIndex(engine)
    fts_index.ensure_schema()
    return MaintenanceService(
        session_factory=create_session_factory(engine),
        fts_index=fts_index,
        vector_index=VectorIndex(settings.chroma_path),
        embedder=EmbeddingClient(
            api_base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            timeout_s=settings.embed_timeout_s,
            max_retries=settings.embed_max_retries,
        ),
        settings=settings,
    )


def run_reindex(
    argv: Sequence[str] | None = None, *, service: MaintenanceService | None = None
) -> int:
    """执行索引重建，输出 JSON 统计。"""

    parser = argparse.ArgumentParser(
        prog="reindex", description="从 SQLite 权威数据重建 FTS5 与 Chroma 索引"
    )
    parser.add_argument(
        "--source-id", type=int, default=None, help="只重建指定来源；省略则全量重建"
    )
    args = parser.parse_args(argv)
    maintenance = service if service is not None else build_maintenance_service(
        get_settings()
    )
    report = (
        maintenance.reindex_source(args.source_id)
        if args.source_id is not None
        else maintenance.reindex_all()
    )
    print(json.dumps(asdict(report), ensure_ascii=False))
    return 0


def run_consistency(
    argv: Sequence[str] | None = None, *, service: MaintenanceService | None = None
) -> int:
    """校验三处数据一致性；不一致时返回退出码 1。"""

    parser = argparse.ArgumentParser(
        prog="check_consistency", description="校验 SQLite/FTS5/Chroma 数据一致性"
    )
    parser.parse_args(argv)
    maintenance = service if service is not None else build_maintenance_service(
        get_settings()
    )
    report = maintenance.check_consistency()
    print(json.dumps(asdict(report), ensure_ascii=False))
    if not report.consistent:
        print("数据不一致：请运行 scripts/reindex.py 重建派生索引")
        return 1
    return 0


def run_backup(
    argv: Sequence[str] | None = None, *, service: MaintenanceService | None = None
) -> int:
    """备份 SQLite 与 Chroma 数据；检测到服务仍在运行时告警。"""

    parser = argparse.ArgumentParser(
        prog="backup", description="备份 SQLite 与 Chroma 数据"
    )
    parser.add_argument(
        "--target", type=Path, default=Path("backups"), help="备份输出目录"
    )
    args = parser.parse_args(argv)
    if service is None:
        settings = get_settings()
        if _is_service_running(settings.app_host, settings.app_port):
            print(
                "警告: 检测到服务正在运行"
                f"（{settings.app_host}:{settings.app_port} 可连接），"
                "Chroma 目录在服务运行中复制可能不一致；"
                "建议停止服务后重新备份（见《项目介绍.md》5.6）。",
                file=sys.stderr,
            )
        maintenance = build_maintenance_service(settings)
    else:
        maintenance = service
    report = maintenance.backup(args.target)
    print(json.dumps(asdict(report), ensure_ascii=False, default=str))
    return 0


def _is_service_running(host: str, port: int, *, timeout_s: float = 0.5) -> bool:
    """探测本机服务端口是否可连接，用于备份前告警。"""

    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False
