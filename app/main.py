"""FastAPI 应用装配。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.logging import configure_logging
from app.core.selfcheck import run_startup_checks
from app.core.settings import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    run_startup_checks(settings)
    app.state.settings = settings
    logger.info("应用启动完成", extra={"stage": "startup"})
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Knowledge Base",
        version="0.1.0",
        description="面向个人 AI 学习的网页知识采集、混合检索与有据问答系统",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    return app


app = create_app()
