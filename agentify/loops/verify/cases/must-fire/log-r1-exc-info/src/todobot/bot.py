"""R1 fires: the skeleton's first leak, rebuilt. The errors handler logged the traceback, and a database
error's DETAIL quotes the rejected row: the user's message."""

from __future__ import annotations

import logging

from aiogram.types import ErrorEvent

from todobot.logs import log_event

logger = logging.getLogger(__name__)


async def on_error(event: ErrorEvent) -> None:
    log_event(
        logger,
        "handler_error",
        level=logging.ERROR,
        update_id=event.update.update_id,
        exc_info=event.exception,
    )
