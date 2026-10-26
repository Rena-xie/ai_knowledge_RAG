"""Cross-encoder rerank API 客户端（v2）。"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from app.domain.cost import (
    ApiUsageAttempt,
    CostUsageObserver,
    estimate_input_units,
    extract_usage_tokens,
)

logger = logging.getLogger(__name__)


class RerankError(RuntimeError):
    """rerank 调用失败；调用方应回退到 RRF 顺序。"""


@dataclass(frozen=True)
class RerankHit:
    """rerank 结果：候选索引与相关性分数。"""

    index: int
    score: float


class RerankClient:
    """调用兼容 OpenAI 风格的 rerank HTTP 接口。"""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        timeout_s: int = 10,
        max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
        usage_observer: CostUsageObserver | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=api_base.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
        )
        self._api_key = api_key
        self._model = model
        self._max_retries = max_retries
        self._usage_observer = usage_observer

    def rerank(
        self, query: str, documents: Sequence[str]
    ) -> list[RerankHit]:
        if not documents:
            return []
        payload = {
            "model": self._model,
            "query": query,
            "documents": list(documents),
            "top_n": len(documents),
            "return_documents": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        estimated_units = estimate_input_units((query, *documents))
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 2):
            self._check_budget(estimated_units)
            recorded = False
            try:
                response = self._client.post(
                    "/rerank", json=payload, headers=headers
                )
                if response.status_code >= 400:
                    self._record_attempt(
                        estimated_units,
                        success=False,
                        attempt=attempt,
                        error_code="RERANK_HTTP_ERROR",
                    )
                    recorded = True
                    raise RerankError(
                        f"rerank HTTP {response.status_code}: "
                        f"{response.text[:200]}"
                    )
                data = response.json()
                results = data.get("results") if isinstance(data, dict) else None
                if not isinstance(results, list):
                    raise RerankError("rerank 响应缺少 results")
                hits = _parse_results(results, document_count=len(documents))
                if not hits:
                    raise RerankError("rerank 未返回有效结果")
                input_units = extract_usage_tokens(data)
                recorded_units = (
                    input_units
                    if input_units is not None
                    else estimated_units
                )
                self._record_attempt(
                    recorded_units,
                    success=True,
                    attempt=attempt,
                    usage_source=(
                        "provider"
                        if input_units is not None
                        else "estimated_characters"
                    ),
                )
                return sorted(hits, key=lambda hit: (-hit.score, hit.index))
            except (httpx.HTTPError, ValueError, RerankError) as error:
                if not recorded:
                    self._record_attempt(
                        estimated_units,
                        success=False,
                        attempt=attempt,
                        error_code="RERANK_FAILED",
                        timed_out=isinstance(error, httpx.TimeoutException),
                    )
                last_error = error
                if attempt <= self._max_retries:
                    time.sleep(0.2 * (2 ** (attempt - 1)))
        raise RerankError(str(last_error or "rerank 失败"))

    def _check_budget(self, input_units: int) -> None:
        if self._usage_observer is None:
            return
        self._usage_observer.check_budget("rerank", input_units)

    def _record_attempt(
        self,
        input_units: int,
        *,
        success: bool,
        attempt: int,
        usage_source: str = "estimated_characters",
        error_code: str | None = None,
        timed_out: bool = False,
    ) -> None:
        if self._usage_observer is None:
            return
        self._usage_observer.record_attempt(
            ApiUsageAttempt(
                operation="rerank",
                model=self._model,
                input_units=input_units,
                usage_source=usage_source,
                success=success,
                attempt=attempt,
                error_code=error_code,
                timed_out=timed_out,
            )
        )

    def close(self) -> None:
        self._client.close()


def _parse_results(
    results: Sequence[object], *, document_count: int
) -> list[RerankHit]:
    hits: list[RerankHit] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        if (
            isinstance(index, int)
            and isinstance(score, (int, float))
            and 0 <= index < document_count
        ):
            hits.append(RerankHit(index=index, score=float(score)))
    return hits
