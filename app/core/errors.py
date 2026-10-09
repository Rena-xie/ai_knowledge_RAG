"""统一错误码与 API 状态映射（见《项目介绍.md》5.2）。"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """全部业务错误码。"""

    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    INGEST_BUSY = "INGEST_BUSY"
    INGEST_TIMEOUT = "INGEST_TIMEOUT"
    SSRF_BLOCKED = "SSRF_BLOCKED"
    FETCH_TIMEOUT = "FETCH_TIMEOUT"
    FETCH_HTTP_ERROR = "FETCH_HTTP_ERROR"
    FETCH_TOO_LARGE = "FETCH_TOO_LARGE"
    FETCH_UNSUPPORTED_CONTENT = "FETCH_UNSUPPORTED_CONTENT"
    PARSE_EMPTY_CONTENT = "PARSE_EMPTY_CONTENT"
    CHUNK_FAILED = "CHUNK_FAILED"
    EMBEDDING_FAILED = "EMBEDDING_FAILED"
    INDEX_WRITE_FAILED = "INDEX_WRITE_FAILED"
    LLM_FAILED = "LLM_FAILED"
    DB_ERROR = "DB_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


HTTP_STATUS_BY_ERROR_CODE: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.INGEST_BUSY: 503,
    ErrorCode.INGEST_TIMEOUT: 504,
    ErrorCode.SSRF_BLOCKED: 403,
    ErrorCode.FETCH_TIMEOUT: 504,
    ErrorCode.FETCH_HTTP_ERROR: 502,
    ErrorCode.FETCH_TOO_LARGE: 413,
    ErrorCode.FETCH_UNSUPPORTED_CONTENT: 415,
    ErrorCode.PARSE_EMPTY_CONTENT: 422,
    ErrorCode.CHUNK_FAILED: 500,
    ErrorCode.EMBEDDING_FAILED: 502,
    ErrorCode.INDEX_WRITE_FAILED: 500,
    ErrorCode.LLM_FAILED: 502,
    ErrorCode.DB_ERROR: 500,
    ErrorCode.INTERNAL_ERROR: 500,
}


class AppError(Exception):
    """带错误码的业务异常，由 API 层统一转换为标准错误响应。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
