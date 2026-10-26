"""请求与后台任务的可观测关联上下文。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)
_JOB_ID: ContextVar[str | None] = ContextVar("job_id", default=None)
_SOURCE_ID: ContextVar[int | None] = ContextVar("source_id", default=None)


@contextmanager
def bind_request_id(request_id: str) -> Iterator[None]:
    token = _REQUEST_ID.set(request_id)
    try:
        yield
    finally:
        _REQUEST_ID.reset(token)


@contextmanager
def bind_job_id(job_id: str) -> Iterator[None]:
    token = _JOB_ID.set(job_id)
    try:
        yield
    finally:
        _JOB_ID.reset(token)


@contextmanager
def bind_source_id(source_id: int) -> Iterator[None]:
    token = _SOURCE_ID.set(source_id)
    try:
        yield
    finally:
        _SOURCE_ID.reset(token)


def current_context() -> tuple[str | None, str | None, int | None]:
    """返回当前 request_id、job_id 和 source_id。"""

    return _REQUEST_ID.get(), _JOB_ID.get(), _SOURCE_ID.get()
