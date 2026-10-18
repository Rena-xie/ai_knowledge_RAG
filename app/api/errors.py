"""API 层统一错误响应（见《项目开发说明_v1.md》8.1）。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import HTTP_STATUS_BY_ERROR_CODE, AppError, ErrorCode

logger = logging.getLogger(__name__)


def error_payload(code: ErrorCode, message: str, request_id: str) -> dict[str, object]:
    """构造统一错误响应体。"""

    return {"error": {"code": code.value, "message": message, "request_id": request_id}}


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", ""))


def register_exception_handlers(app: FastAPI) -> None:
    """注册全部异常处理器，保证错误响应格式一致。"""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        status_code = HTTP_STATUS_BY_ERROR_CODE.get(exc.code, 500)
        return JSONResponse(
            status_code=status_code,
            content=error_payload(exc.code, exc.message, _request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_payload(
                ErrorCode.INVALID_REQUEST, "请求参数不合法", _request_id(request)
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = (
            ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INVALID_REQUEST
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(code, str(exc.detail), _request_id(request)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception(
            "未处理异常: %s", exc, extra={"request_id": _request_id(request)}
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(
                ErrorCode.INTERNAL_ERROR, "服务内部错误", _request_id(request)
            ),
        )
