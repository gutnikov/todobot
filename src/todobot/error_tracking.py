"""todobot's errors in PostHog's error tracking, and a sign of life there at each start (ERR).

- ERR#1: every log line with level ERROR or CRITICAL also goes to PostHog as one `$exception`, made from the line
  itself (`JsonFormatter.fields`, what stdout gets): its log event, its error class, its traceback (`exc`, where our
  own lines have what users wrote cut out) and the version. The live exception is never read: neither its message
  as the exception has it nor a frame's variables leave the process. Nor do the line's other fields (`message`, an
  update's or a user's id).
- ERR#2: `server_started`, with the version, once the server listens.
- Every event: distinct_id `todobot`, `$process_person_profile` false, no GeoIP. Nothing else is sent.
- ERR#3: when PostHog does not take a batch, a WARNING line `posthog_failed`. The SDK's own lines never go above
  WARNING and never go back to PostHog, so a refusing PostHog makes no ERROR line and no loop.

`errors_to_posthog` runs it for the length of `serve`; without POSTHOG_PROJECT_TOKEN nothing of this runs.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any

from posthog import Posthog

from todobot.config import PostHogSettings
from todobot.logs import DEFAULT_EVENT, JsonFormatter, log_event

logger = logging.getLogger(__name__)

DISTINCT_ID = "todobot"  # every event's: no user id goes to PostHog (ERR?2)
EXCEPTION_EVENT = "$exception"
STARTED_EVENT = "server_started"
FAILED_EVENT = "posthog_failed"
SDK_LOGGER = "posthog"  # the logger the SDK writes all its lines to

# Bounds on what a slow or refusing PostHog can cost. A batch PostHog refuses is tried again after 1 s, then 2 s;
# one request waits at most REQUEST_TIMEOUT_SECONDS. Stopping, `serve` waits at most CLOSE_SECONDS for what is queued.
REQUEST_TIMEOUT_SECONDS = 3.0
MAX_RETRIES = 2
FLUSH_INTERVAL_SECONDS = 1.0  # how long an event waits in the queue for others to go with it
CLOSE_SECONDS = 3.0


class ErrorTracking:
    """The PostHog client, and the only way events leave for PostHog: `server_started` and `report`."""

    def __init__(self, settings: PostHogSettings, version: str) -> None:
        self.version = version
        self._client = Posthog(
            settings.token,
            host=settings.host,
            on_error=self._refused,
            flush_interval=FLUSH_INTERVAL_SECONDS,
            max_retries=MAX_RETRIES,
            timeout=REQUEST_TIMEOUT_SECONDS,
            disable_geoip=True,  # the server's address says nothing about users
            enable_exception_autocapture=False,  # errors go through their log line only
            capture_exception_code_variables=False,  # never: a frame's variables hold what users wrote
        )

    def server_started(self) -> None:
        """ERR#2."""
        self._capture(STARTED_EVENT, {})

    def report(self, line: Mapping[str, Any]) -> None:
        """ERR#1: the log line `line` (a JSON line's fields) as one `$exception`."""
        self._capture(EXCEPTION_EVENT, exception_properties(line))

    def close(self) -> None:
        """Send what is queued and stop the SDK's worker, waiting at most CLOSE_SECONDS; what is left then is dropped.

        The SDK's own shutdown waits for as long as PostHog keeps a request open (and its retries); `serve` must stop
        well within the time the platform gives it, so the SDK shuts down on a thread of its own here.
        """
        closer = threading.Thread(target=self._client.shutdown, name="posthog-close", daemon=True)
        closer.start()
        closer.join(CLOSE_SECONDS)
        if closer.is_alive():
            log_event(logger, FAILED_EVENT, level=logging.WARNING, error="TimeoutError", detail="unsent events dropped")

    def _capture(self, event: str, properties: Mapping[str, Any]) -> None:
        self._client.capture(
            event,
            distinct_id=DISTINCT_ID,
            properties={**properties, "version": self.version, "$process_person_profile": False},
        )

    def _refused(self, error: Exception, batch: list[Any]) -> None:
        """The SDK's on_error: PostHog did not take `batch`, also after the retries (ERR#3)."""
        log_event(
            logger,
            FAILED_EVENT,
            level=logging.WARNING,
            error=type(error).__name__,
            status=getattr(error, "status", None),
            events=len(batch),
        )


class PostHogHandler(logging.Handler):
    """Sends each record of level ERROR or above to PostHog as one `$exception`, made from its log line (ERR#1)."""

    def __init__(self, tracking: ErrorTracking) -> None:
        super().__init__(level=logging.ERROR)
        self.tracking = tracking
        self._lines = JsonFormatter(tracking.version)
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        # The SDK's own lines are about PostHog: sending them there could loop. They are WARNING at most anyway.
        if _from_sdk(record) or getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            self.tracking.report(self._lines.fields(record))
        except Exception as exc:  # a line that cannot go to PostHog must not become an error line itself
            log_event(logger, FAILED_EVENT, level=logging.WARNING, error=type(exc).__name__)
        finally:
            self._local.busy = False


class _AtMostWarning(logging.Filter):
    """On the SDK's logger: its lines (a failed upload is one, at ERROR) are WARNING at most (ERR#3)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.WARNING:
            record.levelno = logging.WARNING
            record.levelname = logging.getLevelName(logging.WARNING)
        return True


def _from_sdk(record: logging.LogRecord) -> bool:
    return record.name == SDK_LOGGER or record.name.startswith(f"{SDK_LOGGER}.")


@contextmanager
def errors_to_posthog(settings: PostHogSettings, version: str) -> Iterator[ErrorTracking]:
    """For the length of the block, every ERROR or CRITICAL log line also goes to PostHog. On the way out, what is
    queued is sent (within CLOSE_SECONDS)."""
    sdk_logger = logging.getLogger(SDK_LOGGER)
    if not any(isinstance(existing, _AtMostWarning) for existing in sdk_logger.filters):
        sdk_logger.addFilter(_AtMostWarning())
    tracking = ErrorTracking(settings, version)
    handler = PostHogHandler(tracking)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield tracking
    finally:
        root.removeHandler(handler)
        tracking.close()


# --- A log line as PostHog's `$exception` -------------------------------------------

_TRACEBACK_HEAD = "Traceback (most recent call last):"
_CHAINED = frozenset(
    {
        "The above exception was the direct cause of the following exception:",
        "During handling of the above exception, another exception occurred:",
    }
)
_FRAME = re.compile(r'^  File "(?P<path>.+)", line (?P<lineno>\d+), in (?P<function>.+)$')
_TYPE = re.compile(r"^[A-Za-z_][\w.]*$")
_MARKERS = frozenset(" ^~")  # Python's lines that point at the failing part of the line above
APP_PACKAGE = "todobot"


def exception_properties(line: Mapping[str, Any]) -> dict[str, Any]:
    """The `$exception` properties of a log line: its event, level and logger, its error class and its traceback as
    the line has them, and PostHog's `$exception_list` made of them. Nothing else of the line goes."""
    event = str(line.get("event", DEFAULT_EVENT))
    level = str(line.get("level", "ERROR"))
    logger_name = str(line.get("logger", ""))
    traceback_text = line.get("exc") if isinstance(line.get("exc"), str) else None
    error = line.get("error") if isinstance(line.get("error"), str) else None
    named = error if error and error.isidentifier() else None  # our lines name the class; config_error says what

    exceptions = parse_traceback(traceback_text) if traceback_text else []
    error_class = named or (exceptions[0]["type"] if exceptions else None)
    if not exceptions:  # a line without a traceback Python wrote: what the line says
        label = logger_name if event == DEFAULT_EVENT else event
        exceptions = [{"type": error_class or label, "value": error if error and not named else event}]
    handled = level != "CRITICAL"  # CRITICAL: a crash hook's uncaught exception
    for number, exception in enumerate(exceptions):
        exception["mechanism"] = {"type": "chained" if number else "generic", "handled": handled, "synthetic": False}

    properties: dict[str, Any] = {
        "$exception_list": exceptions,
        "$exception_level": "error" if handled else "fatal",
        "log_event": event,
        "log_level": level,
        "logger": logger_name,
    }
    if error_class:
        properties["error_class"] = error_class
    if traceback_text:
        properties["traceback"] = traceback_text
    return properties


def parse_traceback(text: str) -> list[dict[str, Any]]:
    """The exceptions of a traceback as Python formats it, outermost first (PostHog's order): each its `type`,
    `module`, `value` and `stacktrace`. [] when the text is not one (an exception group, a SyntaxError's caret)."""
    blocks: list[list[str]] = [[]]
    for row in text.splitlines():
        if row in _CHAINED:
            blocks.append([])
        else:
            blocks[-1].append(row)
    exceptions = [_exception(block) for block in blocks]
    if any(exception is None for exception in exceptions):
        return []
    return [exception for exception in reversed(exceptions) if exception is not None]


def _exception(rows: list[str]) -> dict[str, Any] | None:
    """One exception of a chain: its frames, then `module.Type: value` and the value's further lines."""
    frames: list[dict[str, Any]] = []
    described: list[str] = []
    for row in rows:
        if described:
            described.append(row)
        elif not row.strip() or row == _TRACEBACK_HEAD or row.strip().startswith("[Previous line repeated"):
            continue
        elif found := _FRAME.match(row):
            frames.append(_frame(found["path"], int(found["lineno"]), found["function"]))
        elif frames and row.startswith("    "):
            if not set(row) <= _MARKERS and "context_line" not in frames[-1]:
                frames[-1]["context_line"] = row.strip()
        else:
            described.append(row)
    if not described:
        return None
    qualified, _, value = described[0].partition(": ")
    if not _TYPE.match(qualified):
        return None
    module, _, name = qualified.rpartition(".")
    exception: dict[str, Any] = {
        "type": name,
        "module": module or None,
        "value": "\n".join([value, *described[1:]]).strip(),
    }
    if frames:
        exception["stacktrace"] = {"type": "raw", "frames": frames}
    return exception


def _frame(path: str, lineno: int, function: str) -> dict[str, Any]:
    filename = _importable_path(path)
    module = filename.removesuffix(".py").replace("/", ".") if filename != path and filename.endswith(".py") else None
    return {
        "platform": "python",
        "filename": filename,
        "abs_path": path,
        "module": module,
        "function": function,
        "lineno": lineno,
        "in_app": module is not None and (module == APP_PACKAGE or module.startswith(f"{APP_PACKAGE}.")),
    }


def _importable_path(path: str) -> str:
    """`path` from the import root it was found in (`todobot/bot.py`, `aiogram/…`), else as it is."""
    for root in _import_roots():
        if path.startswith(root):
            return path[len(root) :]
    return path


@cache
def _import_roots() -> tuple[str, ...]:
    roots = {f"{Path(entry).absolute()}{os.sep}" for entry in sys.path if entry}
    roots.discard(os.sep)  # the file system's root holds every path: it names no module
    return tuple(sorted(roots, key=len, reverse=True))  # the deepest first: site-packages before its parents
