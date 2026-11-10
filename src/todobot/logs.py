"""JSON lines on stdout: every record, ours or a library's, is one JSON object with `level`, `event`, `version`.

Our own lines carry a named `event` (see `log_event`); a library's line gets `event` = "log" and keeps its text in
`message`. Tracebacks go into the `exc` field of the same object, so they never break the one-object-per-line rule.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import traceback
from collections.abc import Iterable
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

# Attributes every LogRecord has; anything else on a record came in through `extra=`.
_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
# Keys the formatter owns; an `extra` field cannot overwrite them.
_OWN_KEYS = frozenset({"time", "level", "version", "logger"})

DEFAULT_EVENT = "log"

REDACTED = "<redacted>"
# How much of the start of a secret's line identifies it. Renderings that cut a value short keep its start:
# Postgres's "Failing row contains (...)" clips each value to 64 bytes, asyncpg's DataError its repr to 40 chars.
_FRAGMENT_CHARS = 12


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """`text` without the secrets in any form an error message renders them: whole, escaped (repr) or cut short.

    Each line of `text` is cut where a line of a secret begins, so a truncated tail goes too. A short secret can
    cut more than itself; that only shortens the line.
    """
    needles = {
        part[:_FRAGMENT_CHARS]
        for secret in secrets
        if secret
        for form in (secret, repr(secret)[1:-1])
        for line in form.splitlines()
        if (part := line.strip())
    }
    if not needles:
        return text
    lines = []
    for line in text.splitlines(keepends=True):
        cut = min((at for needle in needles if (at := line.find(needle)) != -1), default=-1)
        if cut != -1:
            line = line[:cut] + REDACTED + ("\n" if line.endswith("\n") else "")
        lines.append(line)
    return "".join(lines)


def format_exception(exc: BaseException) -> str:
    """The traceback, as `exc_info` would put it into the `exc` field."""
    return "".join(traceback.format_exception(exc)).rstrip("\n")


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
