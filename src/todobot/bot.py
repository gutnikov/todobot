"""The Telegram side: the Bot (API client), and the dispatcher with the skeleton's handlers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import ErrorEvent, LinkPreviewOptions, Message, TelegramObject, Update
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from todobot import inbox, tasks, texts
from todobot.logs import format_exception, log_event, redact

if TYPE_CHECKING:
    from todobot.trello import TrelloService

logger = logging.getLogger(__name__)

# CAP#2: the command that lists the sender's tasks.
TASKS_COMMAND = "tasks"
# TRL#1: the command that connects the sender's Trello (only with Trello set up).
TRELLO_COMMAND = "trello"


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
    """What the user wrote or sent in a message, and their ids: what is cut out of a traceback before it is logged.

    What users write must not reach the logs. Their ids may, as fields of our own lines, but not inside a traceback:
    a database error's DETAIL quotes the rejected row, the user's id first, and an ERROR line's traceback also goes
    to PostHog, which gets no user id (ERR#1).
    """
    if message is None:
        return ()
    ids = {str(message.chat.id), *([str(message.from_user.id)] if message.from_user else [])}
    # The file_id the inbox keeps: with the bot's token it fetches the user's voice note or photo.
    return (message.text, message.caption, inbox.classify(message).file_id, *sorted(ids))


def build_dispatcher(
    engine: AsyncEngine, *, allowed_user_ids: frozenset[int] | None, trello: TrelloService | None = None
) -> Dispatcher:
    """The dispatcher; `engine` and `trello` reach the handlers as their arguments of those names. With
    `allowed_user_ids` (ACC), only those users are served; None serves every user. With `trello` (TRL), `/trello`
    connects the sender's Trello and `/tasks` reads their board; None: no Trello at all, as CAP says."""
    dispatcher = TodobotDispatcher(engine=engine, trello=trello)
    dispatcher.include_router(build_router(allowed_user_ids=allowed_user_ids, trello=trello is not None))
    return dispatcher


class AllowList(BaseMiddleware):
    """ACC#1: a message from a user the allow list does not name gets texts.REFUSED and nothing else.

    The message observer's inner middleware: it runs once the router has taken the message (a private chat) and a
    handler matched it, before that handler. For a stranger no handler runs, so nothing of the message is stored.
    """

    def __init__(self, user_ids: frozenset[int]) -> None:
        self.user_ids = user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):  # the middleware is only on messages
            return await handler(event, data)
        if event.from_user is not None and event.from_user.id in self.user_ids:
            return await handler(event, data)
        await refuse(event)
        return None


async def refuse(message: Message) -> None:
    """ACC#1: `access_denied`, then the refusal. A refusal Telegram does not take is logged and not apologised for:
    a stranger gets the refusal or nothing."""
    user_id = message.from_user.id if message.from_user else None
    log_event(logger, "access_denied", user_id=user_id, message_id=message.message_id)
    try:
        await message.answer(texts.REFUSED)
    except Exception as exc:
        log_event(
            logger,
            "refusal_failed",
            level=logging.ERROR,
            user_id=user_id,
            error=type(exc).__name__,
            exc=redact(format_exception(exc), _content(message)),
        )


def build_router(*, allowed_user_ids: frozenset[int] | None, trello: bool = False) -> Router:
    router = Router(name="skeleton")
    # The bot is for private chats; anything else is left unhandled.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    if allowed_user_ids is not None:
        router.message.middleware(AllowList(allowed_user_ids))
    # /start is a text message. CommandStart alone also matches a caption, which would swallow a photo or a voice
    # note captioned "/start"; those are stored like any other (SK-2).
    # ignore_mention: in a private chat "/start@name" can only be for us, and checking the name would cost a getMe.
    router.message.register(on_start, F.text, CommandStart(ignore_mention=True))
    # /tasks likewise (CAP#2); it is neither an inbox item nor a task, so it comes before on_message.
    router.message.register(on_tasks, F.text, Command(TASKS_COMMAND, ignore_mention=True))
    # /trello likewise, with Trello set up (TRL#1); without it, it is a command like any other: an inbox item.
    if trello:
        router.message.register(on_trello, F.text, Command(TRELLO_COMMAND, ignore_mention=True))
    router.message.register(on_message)
    router.errors.register(on_error)
    return router


async def on_start(message: Message) -> None:
    """SK-1: the greeting."""
    await message.answer(texts.START)
    log_event(logger, "start", user_id=message.from_user.id if message.from_user else None)


class TasksNotSentError(Exception):
    """Sending the list of tasks failed. It says which message and why, never with the cause's traceback: the tasks are
    what the user wrote, and the cause can quote them (aiogram's ClientDecodeError holds the Bot API's answer, which
    echoes the text sent, and its pydantic error cuts that text short in the middle, where `redact` cannot find it).
    """


async def on_tasks(message: Message, engine: AsyncEngine, trello: TrelloService | None) -> None:
    """CAP#2: the sender's tasks, oldest first, in as many messages as the list needs; stored nowhere. TRL#4: with
    Trello set up, each task as its card on the board is now (`TrelloService.listed`), or that Trello does not answer.
    """
    if message.from_user is None:  # never in a private chat
        return
    user_id = message.from_user.id
    if trello is None:
        listed = [(task.text, texts.STATUS_LABELS[task.status]) for task in await tasks.of_user(engine, user_id)]
    else:
        from_trello = await trello.listed(user_id)
        if from_trello is None:  # Trello does not answer (logged as trello_unavailable)
            await message.answer(texts.TRELLO_DOWN)
            return
        listed = from_trello
    replies = tasks.render_entries(listed)
    for number, reply in enumerate(replies, 1):
        try:
            await message.answer(reply)
        except TelegramAPIError as exc:  # its text is the Bot API's description, which never quotes the text sent
            raise TasksNotSentError(f"message {number} of {len(replies)}: {type(exc).__name__}: {exc}") from None
        except Exception as exc:  # anything else may quote it (see TasksNotSentError): its type only
            raise TasksNotSentError(f"message {number} of {len(replies)}: {type(exc).__name__}") from None
    log_event(logger, "tasks_listed", user_id=user_id, tasks=len(listed), messages=len(replies))


async def on_trello(message: Message, trello: TrelloService) -> None:
    """TRL#1: the link to Trello's page that asks the sender to give the bot access to their Trello. Stored nowhere but
    as the request it starts (`todobot.trello.connect`). The link holds the request token: it is never logged, and
    Telegram is not asked to fetch it for a preview."""
    if message.from_user is None:  # never in a private chat
        return
    user_id = message.from_user.id
    link = await trello.link(user_id)
    await message.answer(texts.TRELLO_LINK.format(link=link), link_preview_options=LinkPreviewOptions(is_disabled=True))
    log_event(logger, "trello_link_sent", user_id=user_id)


async def on_message(message: Message, engine: AsyncEngine, trello: TrelloService | None) -> None:
    """SK-2: any other message (other commands included) goes to the sender's inbox; CAP#1: a plain text becomes a
    task with it (`inbox.save`).

    DUP#1: Telegram's repeat of an update (the same chat and message_id) finds its message stored; it is kept once
    and not replied to again, and makes no second task. TRL#3: with Trello set up, a new task's card is made outside
    the webhook, by the loop this nudges; the reply does not wait for it.
    """
    if message.from_user is None:  # never in a private chat; there would be no one to store it for
        return
    user_id = message.from_user.id
    item = inbox.classify(message)
    if not await inbox.save(engine, user_id, message.message_id, item):
        log_event(logger, "inbox_repeat", kind=item.kind, user_id=user_id, message_id=message.message_id)
        return
    log_event(logger, "inbox_saved", kind=item.kind, user_id=user_id, message_id=message.message_id)
    if item.task_text is not None:
        log_event(logger, "task_recorded", user_id=user_id, message_id=message.message_id)
        if trello is not None:  # TRL#3: its card is made outside the webhook, starting now
            trello.nudge()
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
