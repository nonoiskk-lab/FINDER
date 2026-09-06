"""Structured logging and run bookkeeping."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s  %(levelname)-7s  %(name)-28s  %(message)s",
                         datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = getattr(record, "extra_fields", {})
        if extra:
            trail = " ".join(f"{k}={v}" for k, v in extra.items())
            return f"{base}  [{trail}]"
        return base


def setup_logging(level: str = "INFO", json_output: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else HumanFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Third-party noise we never want in an operator's terminal.
    for noisy in ("urllib3", "googleapiclient", "google_auth_httplib2", "pdfminer",
                  "httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> BoundLogger:
    return BoundLogger(logging.getLogger(name))


class BoundLogger:
    """Thin wrapper giving every call site keyword-style structured fields."""

    def __init__(self, logger: logging.Logger, **bound: Any) -> None:
        self._logger = logger
        self._bound = bound

    def bind(self, **fields: Any) -> BoundLogger:
        return BoundLogger(self._logger, **{**self._bound, **fields})

    def _log(self, level: int, message: str, exc_info: bool = False, **fields: Any) -> None:
        self._logger.log(
            level, message, exc_info=exc_info,
            extra={"extra_fields": {**self._bound, **fields}},
        )

    def debug(self, message: str, **f: Any) -> None:
        self._log(logging.DEBUG, message, **f)

    def info(self, message: str, **f: Any) -> None:
        self._log(logging.INFO, message, **f)

    def warning(self, message: str, **f: Any) -> None:
        self._log(logging.WARNING, message, **f)

    def error(self, message: str, exc_info: bool = False, **f: Any) -> None:
        self._log(logging.ERROR, message, exc_info=exc_info, **f)

    def exception(self, message: str, **f: Any) -> None:
        self._log(logging.ERROR, message, exc_info=True, **f)


def new_run_id(now: datetime | None = None, tz: str = "Asia/Kolkata") -> str:
    stamp = (now or datetime.now(ZoneInfo(tz))).strftime("%Y%m%dT%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:6]}"


def now_ist(tz: str = "Asia/Kolkata") -> datetime:
    return datetime.now(ZoneInfo(tz))
