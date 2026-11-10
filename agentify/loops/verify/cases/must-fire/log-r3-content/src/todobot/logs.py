"""A minimal logs.py: the project's helpers, as src/todobot/logs.py has them."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Iterable
from typing import Any


def redact(text: str, secrets: Iterable[str | None]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def format_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc)).rstrip("\n")


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: BaseException | bool | None = None,
    **fields: Any,
) -> None:
    logger.log(level, event, exc_info=exc_info, extra={"event": event, **fields})
