"""FastAPI 应用装配。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.costs import router as costs_router
from app.api.deps import Container
from app.api.errors import register_exception_handlers
from app.api.health import router as health_router
from app.api.ingestions import router as ingestions_router
from app.api.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from app.api.search import router as search_router
from app.api.sources import router as sources_router
from app.api.stats import router as stats_router
from app.core.errors import ErrorCode
from app.core.logging import configure_logging
from app.core.selfcheck import run_startup_checks
from app.core.settings import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = getattr(app.state, "settings", None) or get_settings()
    configure_logging(settings.log_level, log_file=settings.log_file)
    run_startup_checks(settings)
    container = getattr(app.state, "container", None)
    if container is None:
        container = Container(settings)
    app.state.settings = settings
    app.state.container = container
    recovered = container.ingestion_service.recover_interrupted()
    if recovered:
        logger.warning(
            "回收中断的入库记录: %s",
            list(recovered),
            extra={
                "stage": "startup",
                "error_code": ErrorCode.INGEST_INTERRUPTED.value,
            },
        )
    container.start_background_tasks()
    logger.info("应用启动完成", extra={"stage": "startup"})
    yield
    container.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="ai-knowledge-rag",
        version="2.0.0",
        description="面向个人 AI 学习的网页知识采集、混合检索与有据问答系统",
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    app.include_router(sources_router)
    app.include_router(ingestions_router)
    app.include_router(search_router)
    app.include_router(stats_router)
    app.include_router(costs_router)
    return app


app = create_app()
