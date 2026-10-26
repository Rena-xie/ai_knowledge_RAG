"""外部 API 用量与成本观测的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class CostBudgetExceeded(RuntimeError):
    """配置的 API 成本预算已不足以支付下一次调用。"""


@dataclass(frozen=True)
class ApiUsageAttempt:
    """一次外部 API HTTP 尝试的用量信息。"""

    operation: str
    model: str
    input_units: int
    usage_source: str
    success: bool
    attempt: int
    error_code: str | None = None
    timed_out: bool = False


@dataclass(frozen=True)
class CostAggregate:
    """按操作、模型和用量来源聚合后的成本。"""

    operation: str
    provider: str
    model: str
    usage_source: str
    currency: str
    price_per_million: float | None
    input_units: int
    cost: float | None
    request_count: int
    success_count: int
    failure_count: int
    retry_count: int
    timeout_count: int
    uncosted_count: int


class CostUsageObserver(Protocol):
    """API 客户端可依赖的最小用量观测协议。"""

    provider: str

    def check_budget(self, operation: str, input_units: int) -> None:
        """在调用供应商前检查预算。"""

    def record_attempt(self, attempt: ApiUsageAttempt) -> None:
        """记录一次 HTTP 尝试。"""


def estimate_input_units(texts: list[str] | tuple[str, ...]) -> int:
    """供应商未返回 usage 时，按字符数给出保守的输入量估算。"""

    return max(1, sum(len(text) for text in texts))


def extract_usage_tokens(payload: object) -> int | None:
    """从常见 OpenAI 风格 usage 字段中读取 token 总量。"""

    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    for key in ("total_tokens", "prompt_tokens", "input_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None
