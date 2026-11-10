"""What the rule must leave alone: the redacting form, ids, and the project's own create_engine."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.types import ErrorEvent, Message

from todobot.db import create_engine
from todobot.logs import format_exception, log_event, redact

logger = logging.getLogger(__name__)


def _content(message: Message | None) -> tuple[str | None, ...]:
    return (message.text, message.caption) if message else ()


def _update_id(update: object) -> int | None:
    value = update.get("update_id") if isinstance(update, dict) else None
    return value if isinstance(value, int) else None


async def on_error(event: ErrorEvent) -> None:
    message = event.update.message
    log_event(
        logger,
        "handler_error",
        level=logging.ERROR,
        update_id=event.update.update_id,
        error=type(event.exception).__name__,
        exc=redact(format_exception(event.exception), (message.text, message.caption) if message else ()),
    )


def on_invalid(update: dict[str, Any], exc: Exception) -> None:
    log_event(logger, "update_invalid", update_id=_update_id(update), error=type(exc).__name__)


async def on_message(message: Message) -> None:
    length = len(message.text or "")
    log_event(logger, "inbox_saved", user_id=message.from_user.id if message.from_user else None, length=length)
    logger.info("chat %s", message.chat.id)


async def watch(task: asyncio.Task[None]) -> None:
    await asyncio.wait([task])
    if task.exception() is not None:
        log_event(logger, "task_failed", error=type(task.exception()).__name__)


def engine_url() -> str:
    return str(create_engine("postgresql+asyncpg://u:p@localhost/db").url)
