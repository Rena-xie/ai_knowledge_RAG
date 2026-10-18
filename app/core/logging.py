"""结构化日志配置（字段约定见《项目开发说明_v1.md》10.5）。"""

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

EXTRA_FIELDS = ("request_id", "source_id", "stage", "duration_ms", "error_code")
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


class JsonFormatter(logging.Formatter):
    """将日志渲染为单行 JSON，并保留约定的上下文字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # 使用 ASCII 转义输出，避免 Windows 控制台代码页导致中文日志乱码或编码异常。
        return json.dumps(payload)


def configure_logging(
    level: str = "INFO",
    *,
    log_file: str | Path | None = None,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> None:
    """重置根日志配置：单行 JSON 输出到标准输出，可选按大小滚动的文件。

    ``log_file`` 为空或未提供时只写标准输出。
    """

    formatter = JsonFormatter()
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    root.setLevel(level.upper())
