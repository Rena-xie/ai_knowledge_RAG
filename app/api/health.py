"""健康检查接口：/health/live 与 /health/ready。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.selfcheck import SelfCheckError, run_startup_checks

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict[str, str]:
    """进程存活检查。"""

    return {"status": "ok"}


@router.get("/health/ready")
def ready(request: Request) -> JSONResponse:
    """就绪检查：SQLite 与 Chroma 存储可用。"""

    settings = request.app.state.settings
    try:
        run_startup_checks(settings)
    except SelfCheckError as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": str(exc)},
        )
    return JSONResponse(
        content={
            "status": "ok",
            "checks": {"sqlite": "ok", "fts5": "ok", "chroma": "ok"},
        }
    )
