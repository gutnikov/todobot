"""R3 fires: what the user wrote goes into a log line, as a field and as the whole message."""

from __future__ import annotations

import logging

from aiogram.types import Message

from todobot.logs import log_event

logger = logging.getLogger(__name__)


async def on_message(message: Message) -> None:
    log_event(logger, "inbox_saved", text=message.text)
    logger.info("got %s", message)
