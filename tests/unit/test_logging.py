"""结构化日志格式（见《项目介绍.md》10.5）。"""

import json
import logging

from app.core.logging import JsonFormatter


def test_json_formatter_emits_ascii_safe_json() -> None:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="应用启动完成",
        args=(),
        exc_info=None,
    )
    record.stage = "startup"
    record.duration_ms = 12

    payload = JsonFormatter().format(record)

    payload.encode("ascii")  # 纯 ASCII 输出，避免 Windows 控制台代码页问题
    data = json.loads(payload)
    assert data["level"] == "INFO"
    assert data["message"] == "应用启动完成"
    assert data["stage"] == "startup"
    assert data["duration_ms"] == 12
