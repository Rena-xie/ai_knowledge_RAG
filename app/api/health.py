"""健康检查接口：/health/live 与 /health/ready。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine

from app.core.selfcheck import (
    check_fts5_available,
    ensure_ascii_path,
    ensure_writable_dir,
)
from app.infra.vector import VectorIndex

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict[str, str]:
    """进程存活检查。"""

    return {"status": "ok"}


@router.get("/health/ready")
def ready(request: Request) -> JSONResponse:
    """就绪检查：SQLite 可连接、FTS5 可用、Chroma 可查询。

    失败时只返回检查项状态，异常细节写入日志，避免把内部路径暴露给调用方。
    """

    settings = request.app.state.settings
    container = request.app.state.container
    checks = {"sqlite": "ok", "fts5": "ok", "chroma": "ok"}
    actions = {
        "sqlite": lambda: _ping_sqlite(container.engine, settings.sqlite_path),
        "fts5": lambda: check_fts5_available(settings.sqlite_path),
        "chroma": lambda: _ping_chroma(
            container.vector_index, settings.chroma_path
        ),
    }
    for name, action in actions.items():
        try:
            action()
        except Exception:
            checks[name] = "error"
            logger.exception(
                "就绪检查未通过", extra={"stage": "health", "check": name}
            )
    healthy = "error" not in checks.values()
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "error", "checks": checks},
    )


def _ping_sqlite(engine: Engine, db_path: Path) -> None:
    ensure_writable_dir(db_path.parent)
    with engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")


def _ping_chroma(vector_index: VectorIndex, chroma_path: Path) -> None:
    ensure_ascii_path(chroma_path)
    ensure_writable_dir(chroma_path)
    vector_index.count()
