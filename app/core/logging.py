"""结构化日志配置（字段约定见《项目介绍.md》10.5）。"""

import json
import logging

EXTRA_FIELDS = ("request_id", "source_id", "stage", "duration_ms", "error_code")


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


def configure_logging(level: str = "INFO") -> None:
    """重置根日志配置，输出单行 JSON 到标准输出。"""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
