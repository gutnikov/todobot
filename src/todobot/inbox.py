"""Taking in a message (SK-2): what kind it is, and storing it in the sender's inbox once (DUP#1), with the task it
becomes when it is a plain text (CAP#1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from aiogram.types import Message
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from todobot import tasks
from todobot.schema import inbox, users

Kind = Literal["text", "voice", "photo", "other"]

# CAP's seams: a text that starts with this is a command.
COMMAND_PREFIX = "/"


@dataclass(frozen=True, slots=True)
class InboxItem:
    kind: Kind
    text: str | None
    file_id: str | None

    @property
    def task_text(self) -> str | None:
        """CAP#1: the text of the task this item becomes: a plain text that is not a command becomes one, whole,
        however many things it names. None for anything else: a command, a voice note, a photo (caption or not)."""
        if self.kind == "text" and self.text is not None and not self.text.startswith(COMMAND_PREFIX):
            return self.text
        return None


def classify(message: Message) -> InboxItem:
    """The SK-2 table: text, voice, photo (its largest size, the last one), and everything else as `other`."""
    if message.text is not None:
        return InboxItem(kind="text", text=message.text, file_id=None)
    if message.voice is not None:
        return InboxItem(kind="voice", text=message.caption, file_id=message.voice.file_id)
    if message.photo:
        return InboxItem(kind="photo", text=message.caption, file_id=message.photo[-1].file_id)
    return InboxItem(kind="other", text=message.caption, file_id=None)


async def save(engine: AsyncEngine, user_id: int, message_id: int, item: InboxItem) -> bool:
    """One `inbox` row for the user's message, in one transaction; the user's row is created on their first message,
    and the item's task (CAP#1, `InboxItem.task_text`) is recorded with it.

    The message is the user's private chat and its `message_id` (DUP#1). True when this stored it; False when it was
    stored before (Telegram sent the same update again), and then nothing changes: no second item, no second task.
    """
    async with engine.begin() as conn:
        await conn.execute(pg_insert(users).values(id=user_id).on_conflict_do_nothing(index_elements=[users.c.id]))
        stored = await conn.execute(
            pg_insert(inbox)
            .values(user_id=user_id, message_id=message_id, kind=item.kind, text=item.text, file_id=item.file_id)
            .on_conflict_do_nothing(index_elements=[inbox.c.user_id, inbox.c.message_id])
            .returning(inbox.c.message_id)
        )
        if stored.first() is None:
            return False
        if (task_text := item.task_text) is not None:
            await tasks.record(conn, user_id, message_id, task_text)
        return True
