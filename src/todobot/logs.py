"""JSON lines on stdout: every record, ours or a library's, is one JSON object with `level`, `event`, `version`.

Our own lines carry a named `event` (see `log_event`); a library's line gets `event` = "log" and keeps its text in
`message`. Tracebacks go into the `exc` field of the same object, so they never break the one-object-per-line rule.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

# Attributes every LogRecord has; anything else on a record came in through `extra=`.
_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
# Keys the formatter owns; an `extra` field cannot overwrite them.
_OWN_KEYS = frozenset({"time", "level", "version", "logger"})

DEFAULT_EVENT = "log"


class JsonFormatter(logging.Formatter):
    def __init__(self, version: str) -> None:
        super().__init__()
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": DEFAULT_EVENT,
            "version": self.version,
            "logger": record.name,
        }
        for key, value in vars(record).items():
            if key not in _RECORD_ATTRS and key not in _OWN_KEYS:
                payload[key] = value
        if message != payload["event"]:
            payload["message"] = message
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


class _AccessEvent(logging.Filter):
    """Names aiohttp's access-log lines, which otherwise would be anonymous `log` events."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.event = "http_request"
        return True


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: BaseException | bool | None = None,
    **fields: Any,
) -> None:
    """One of our own log lines: `event` names what happened, `fields` add context."""
    logger.log(level, event, exc_info=exc_info, extra={"event": event, **fields})


def setup_logging(version: str, level: int = logging.INFO) -> None:
    """Route all stdlib logging, warnings and uncaught exceptions to stdout as JSON lines."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(version))

    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)

    logging.getLogger("aiohttp.access").addFilter(_AccessEvent())
    logging.captureWarnings(True)

    crash_logger = logging.getLogger("todobot.crash")

    def excepthook(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:  # pragma: no cover - only on a crash
        log_event(crash_logger, "uncaught_exception", level=logging.CRITICAL, exc_info=exc)

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:  # pragma: no cover - only on a crash
        if args.exc_value is not None:
            log_event(crash_logger, "uncaught_exception", level=logging.CRITICAL, exc_info=args.exc_value)

    def unraisablehook(unraisable: sys.UnraisableHookArgs) -> None:  # pragma: no cover - only on a crash
        log_event(
            crash_logger,
            "unraisable_exception",
            level=logging.ERROR,
            exc_info=unraisable.exc_value,
            detail=unraisable.err_msg,
            object=repr(unraisable.object),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    sys.unraisablehook = unraisablehook
