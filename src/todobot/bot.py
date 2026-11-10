"""The Telegram side: the Bot (API client), and the dispatcher with the skeleton's handlers."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import ErrorEvent, Message
from sqlalchemy.ext.asyncio import AsyncEngine

from todobot import inbox, texts
from todobot.logs import format_exception, log_event, redact

logger = logging.getLogger(__name__)


def build_bot(token: str, api_base: str) -> Bot:
    """A Bot API client for `{api_base}/bot{token}/{method}`. Creating it makes no API call."""
    return Bot(token=token, session=AiohttpSession(api=TelegramAPIServer.from_base(api_base)))


def build_dispatcher(engine: AsyncEngine) -> Dispatcher:
    """The dispatcher; `engine` reaches the handlers as their `engine` argument."""
    dispatcher = Dispatcher(engine=engine)
    dispatcher.include_router(build_router())
    return dispatcher


def build_router() -> Router:
    router = Router(name="skeleton")
    # The bot is for private chats; anything else is left unhandled.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    # ignore_mention: in a private chat "/start@name" can only be for us, and checking the name would cost a getMe.
    router.message.register(on_start, CommandStart(ignore_mention=True))
    router.message.register(on_message)
    router.errors.register(on_error)
    return router


async def on_start(message: Message) -> None:
    """SK-1: the greeting."""
    await message.answer(texts.START)
    log_event(logger, "start", user_id=message.from_user.id if message.from_user else None)


async def on_message(message: Message, engine: AsyncEngine) -> None:
    """SK-2: any other message (other commands included) goes to the sender's inbox."""
    if message.from_user is None:  # never in a private chat; there would be no one to store it for
        return
    item = inbox.classify(message)
    await inbox.save(engine, message.from_user.id, item)
    log_event(logger, "inbox_saved", kind=item.kind, user_id=message.from_user.id)
    await message.answer(texts.SAVED)


async def on_error(event: ErrorEvent, bot: Bot) -> None:
    """SK-5: any exception a handler raises is logged and apologised for; the update counts as handled.

    What users write stays out of the logs. The traceback can quote it: a database error's DETAIL shows the
    rejected row. So the message's text and caption are cut out of it.
    """
    message = event.update.message
    content = (message.text, message.caption) if message else ()
    log_event(
        logger,
        "handler_error",
        level=logging.ERROR,
        update_id=event.update.update_id,
        error=type(event.exception).__name__,
        exc=redact(format_exception(event.exception), content),
    )
    if message is None:
        return
    try:
        await bot.send_message(chat_id=message.chat.id, text=texts.ERROR)
    except Exception as exc:  # the apology is best effort: the error is logged above either way
        log_event(logger, "apology_failed", level=logging.ERROR, exc_info=exc, update_id=event.update.update_id)
