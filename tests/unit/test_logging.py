"""结构化日志格式（见《项目介绍.md》10.5）。"""

import json
import logging
from pathlib import Path

from app.core.logging import JsonFormatter, configure_logging


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


def test_configure_logging_writes_rolling_file(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "app.log"

    configure_logging("INFO", log_file=log_path)
    logging.getLogger("app.test").warning("写入文件", extra={"stage": "unit"})

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(lines[-1])
    assert payload["message"] == "写入文件"
    assert payload["stage"] == "unit"


def test_configure_logging_without_file_uses_stdout_only() -> None:
    configure_logging("INFO", log_file="")

    assert len(logging.getLogger().handlers) == 1
