"""运维命令：重建索引（见《项目介绍.md》5.4）。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict

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
