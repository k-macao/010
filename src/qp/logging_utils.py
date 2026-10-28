"""One-line JSON logs: greppable, ingestible by Loki/ELK, no extra dependency."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

_CONFIGURED = False


def get_logger(name: str = "qp") -> logging.Logger:
    return logging.getLogger(name)


def setup_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.environ.get("QP_LOG_LEVEL", level).upper())
    _CONFIGURED = True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def log_event(logger: logging.Logger, level: int, msg: str, **context: Any) -> None:
    logger.log(level, msg, extra={"context": context})
