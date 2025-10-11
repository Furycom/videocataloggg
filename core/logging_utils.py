from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

from .paths import get_logs_dir, resolve_working_dir


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload: Dict[str, Any] = {
            "ts": time.time(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "__dict__", {}).items():
            if key.startswith("_"):
                continue
            if key in payload:
                continue
            try:
                json.dumps(value)
            except TypeError:
                continue
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_json_logging(name: str = "videocatalog") -> logging.Logger:
    working_dir = resolve_working_dir()
    logs_dir = get_logs_dir(working_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "videocatalog.log.jsonl"
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", None) == str(log_path):
            break
    else:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(JsonLogFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def redact_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-2:]}"
