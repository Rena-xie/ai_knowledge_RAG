"""Embedding 客户端（SiliconFlow，见《项目开发说明_v1.md》10.4）。"""

from __future__ import annotations

import time
from collections.abc import Sequence

import httpx

from app.core.errors import ErrorCode

MAX_BATCH_SIZE = 32
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class EmbeddingError(Exception):
    """向量化失败。"""

    def __init__(
        self, message: str, *, code: ErrorCode = ErrorCode.EMBEDDING_FAILED
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EmbeddingClient:
    """按批调用 Embedding API，返回与输入顺序一致的向量。"""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        timeout_s: float,
        max_retries: int,
        backoff_s: float = 1.0,
        batch_size: int = MAX_BATCH_SIZE,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._batch_size = batch_size
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def embed(
        self, texts: Sequence[str], *, budget_s: float | None = None
    ) -> list[list[float]]:
        """按批向量化；``budget_s`` 为本次调用可用的剩余时间预算（秒）。

        传入预算后，每批请求的超时与重试退避都受该预算约束，预算耗尽立即
        按 `INGEST_TIMEOUT` 失败，保证调用方的时间预算不会被批次数量放大。
        """

        items = list(texts)
        deadline = None if budget_s is None else time.monotonic() + budget_s
        vectors: list[list[float]] = []
        for start in range(0, len(items), self._batch_size):
            vectors.extend(
                self._embed_batch(items[start : start + self._batch_size], deadline)
            )
        return vectors

    def close(self) -> None:
        self._client.close()

    def _embed_batch(
        self, batch: Sequence[str], deadline: float | None
    ) -> list[list[float]]:
        payload = {"model": self._model, "input": list(batch)}
        last_error: EmbeddingError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(
                    f"{self._api_base}/embeddings",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=self._next_timeout(deadline),
                )
            except httpx.TimeoutException as error:
                last_error = EmbeddingError(f"Embedding 调用超时: {error}")
            except httpx.HTTPError as error:
                last_error = EmbeddingError(f"Embedding 调用失败: {error}")
            else:
                if response.status_code == 200:
                    return self._parse(response)
                last_error = EmbeddingError(
                    f"Embedding 返回 HTTP {response.status_code}"
                )
                if response.status_code not in RETRYABLE_STATUSES:
                    raise last_error
            if attempt < self._max_retries and self._backoff_s:
                self._sleep_backoff(attempt, deadline)
        raise last_error or EmbeddingError("Embedding 调用失败")

    def _next_timeout(self, deadline: float | None) -> float:
        if deadline is None:
            return self._timeout_s
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EmbeddingError("入库时间预算已用尽", code=ErrorCode.INGEST_TIMEOUT)
        return min(self._timeout_s, remaining)

    def _sleep_backoff(self, attempt: int, deadline: float | None) -> None:
        delay = self._backoff_s * (2**attempt)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            delay = min(delay, remaining)
        time.sleep(delay)

    @staticmethod
    def _parse(response: httpx.Response) -> list[list[float]]:
        try:
            items = sorted(response.json()["data"], key=lambda item: item["index"])
            return [[float(value) for value in item["embedding"]] for item in items]
        except (KeyError, TypeError, ValueError) as error:
            raise EmbeddingError(f"Embedding 响应格式不正确: {error}") from error
