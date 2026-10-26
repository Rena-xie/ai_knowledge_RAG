"""结构化日志格式（见《项目开发说明_v1.md》10.5）。"""

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
    record.job_id = "job_test"
    record.source_id = 7
    record.operation = "embedding"
    record.provider = "siliconflow"
    record.model = "BAAI/bge-m3"
    record.input_units = 12
    record.usage_source = "provider"
    record.currency = "CNY"
    record.cost = 0.000001
    record.success = True
    record.attempt = 1
    record.timed_out = False
    record.api_key = "must-not-leak"

    payload = JsonFormatter().format(record)

    payload.encode("ascii")  # 纯 ASCII 输出，避免 Windows 控制台代码页问题
    data = json.loads(payload)
    assert data["level"] == "INFO"
    assert data["message"] == "应用启动完成"
    assert data["stage"] == "startup"
    assert data["duration_ms"] == 12
    assert data["job_id"] == "job_test"
    assert data["source_id"] == 7
    assert data["operation"] == "embedding"
    assert data["input_units"] == 12
    assert data["cost"] == 0.000001
    assert "api_key" not in data


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
