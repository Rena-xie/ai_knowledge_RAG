"""错误码与 HTTP 映射的完整性（见《项目开发说明_v1.md》5.2）。"""

from app.core.errors import HTTP_STATUS_BY_ERROR_CODE, ErrorCode


def test_every_error_code_has_http_mapping() -> None:
    assert set(ErrorCode) == set(HTTP_STATUS_BY_ERROR_CODE)


def test_mapping_matches_design_doc() -> None:
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.INVALID_REQUEST] == 400
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.REQUEST_TOO_LARGE] == 413
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.NOT_FOUND] == 404
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.INGEST_BUSY] == 503
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.INGEST_TIMEOUT] == 504
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.INGEST_INTERRUPTED] == 500
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.SSRF_BLOCKED] == 403
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.ROBOTS_DISALLOWED] == 403
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.FETCH_TOO_LARGE] == 413
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.FETCH_UNSUPPORTED_CONTENT] == 415
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.PARSE_EMPTY_CONTENT] == 422
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.EMBEDDING_FAILED] == 502
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.LLM_FAILED] == 502
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.RETRIEVAL_UNAVAILABLE] == 503
    assert HTTP_STATUS_BY_ERROR_CODE[ErrorCode.DB_ERROR] == 500
