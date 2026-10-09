"""时间工具：统一使用 UTC ISO-8601 秒级时间戳。"""

from datetime import UTC, datetime


def utc_now_iso() -> str:
    """返回形如 2026-10-09T12:00:00Z 的 UTC 时间戳。"""

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
