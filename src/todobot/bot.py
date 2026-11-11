"""The Telegram side: the Bot (API client), and the dispatcher with the skeleton's handlers."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import ErrorEvent, Message, Update
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from todobot import inbox, texts
from todobot.logs import format_exception, log_event, redact

logger = logging.getLogger(__name__)


def build_bot(token: str, api_base: str) -> Bot:
    """A Bot API client for `{api_base}/bot{token}/{method}`. Creating it makes no API call."""
    return Bot(token=token, session=AiohttpSession(api=TelegramAPIServer.from_base(api_base)))


class TodobotDispatcher(Dispatcher):
    """aiogram's Dispatcher, except that what users write cannot reach the logs on the way in, and nothing raises.

    The webhook (`todobot.web.WebhookHandler`) feeds every update to `feed_raw_update` and answers once it returns.
    Out of the box, an update that does not validate raises a ValidationError, whose message quotes the input: the
    user's text. Here it becomes one `update_invalid` line with field locations and error types only. An exception
    that escapes the errors handler (none should) is logged likewise, without the message's content.
    """

    async def feed_raw_update(self, bot: Bot, update: dict[str, Any], **kwargs: Any) -> Any:
        try:
            parsed = Update.model_validate(update, context={"bot": bot})
        except ValidationError as exc:
            problems = exc.errors(include_url=False, include_context=False, include_input=False)
            log_event(
                logger,
                "update_invalid",
                level=logging.ERROR,
                update_id=update_id_of(update),
                error=type(exc).__name__,
                problems=[
                    f"{'.'.join(map(str, problem['loc'])) or 'update'}: {problem['type']}" for problem in problems
                ],
            )
            return None
        try:
            return await self.feed_update(bot, parsed, **kwargs)
        except Exception as exc:
            log_event(
                logger,
                "update_failed",
                level=logging.ERROR,
                update_id=parsed.update_id,
                error=type(exc).__name__,
                exc=redact(format_exception(exc), _content(parsed.message)),
            )
            return None


def update_id_of(update: object) -> int | None:
    """The `update_id` of a raw update, when it has a valid one."""
    value = update.get("update_id") if isinstance(update, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _content(message: Message | None) -> tuple[str | None, ...]:
    """What the user wrote in a message, and their ids: what is cut out of a traceback before it is logged.

    What users write must not reach the logs. Their ids may, as fields of our own lines, but not inside a traceback:
    a database error's DETAIL quotes the rejected row, the user's id first, and an ERROR line's traceback also goes
    to PostHog, which gets no user id (ERR#1).
    """
    if message is None:
        return ()
    ids = {str(message.chat.id), *([str(message.from_user.id)] if message.from_user else [])}
    return (message.text, message.caption, *sorted(ids))


def build_dispatcher(engine: AsyncEngine) -> Dispatcher:
    """The dispatcher; `engine` reaches the handlers as their `engine` argument."""
    dispatcher = TodobotDispatcher(engine=engine)
    dispatcher.include_router(build_router())
    return dispatcher


def build_router() -> Router:
    router = Router(name="skeleton")
    # The bot is for private chats; anything else is left unhandled.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    # /start is a text message. CommandStart alone also matches a caption, which would swallow a photo or a voice
    # note captioned "/start"; those are stored like any other (SK-2).
    # ignore_mention: in a private chat "/start@name" can only be for us, and checking the name would cost a getMe.
    router.message.register(on_start, F.text, CommandStart(ignore_mention=True))
    router.message.register(on_message)
    router.errors.register(on_error)
    return router


async def on_start(message: Message) -> None:
    """SK-1: the greeting."""
    await message.answer(texts.START)
    log_event(logger, "start", user_id=message.from_user.id if message.from_user else None)


async def on_message(message: Message, engine: AsyncEngine) -> None:
    """SK-2: any other message (other commands included) goes to the sender's inbox.

    DUP#1: Telegram's repeat of an update (the same chat and message_id) finds its message stored; it is kept once
    and not replied to again.
    """
    if message.from_user is None:  # never in a private chat; there would be no one to store it for
        return
    user_id = message.from_user.id
    item = inbox.classify(message)
    if not await inbox.save(engine, user_id, message.message_id, item):
        log_event(logger, "inbox_repeat", kind=item.kind, user_id=user_id, message_id=message.message_id)
        return
    log_event(logger, "inbox_saved", kind=item.kind, user_id=user_id, message_id=message.message_id)
    await message.answer(texts.SAVED)


async def on_error(event: ErrorEvent, bot: Bot) -> None:
    """SK-5: any exception a handler raises is logged and apologised for; the update counts as handled.

    What users write stays out of the logs. The traceback can quote it: a database error's DETAIL shows the
    rejected row. So the message's text and caption, and the user's ids, are cut out of it (`_content`).
    """
    message = event.update.message
    log_event(
        logger,
        "handler_error",
        level=logging.ERROR,
        update_id=event.update.update_id,
        error=type(event.exception).__name__,
        exc=redact(format_exception(event.exception), _content(message)),
    )
    if message is None:
        return
    try:
        await bot.send_message(chat_id=message.chat.id, text=texts.ERROR)
    except Exception as exc:  # the apology is best effort: the error is logged above either way
        log_event(
            logger,
            "apology_failed",
            level=logging.ERROR,
            update_id=event.update.update_id,
            error=type(exc).__name__,
            exc=redact(format_exception(exc), _content(message)),
        )
