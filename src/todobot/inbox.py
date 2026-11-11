"""Taking in a message (SK-2): what kind it is, and storing it in the sender's inbox once (DUP#1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from aiogram.types import Message
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from todobot.schema import inbox, users

Kind = Literal["text", "voice", "photo", "other"]


@dataclass(frozen=True, slots=True)
class InboxItem:
    kind: Kind
    text: str | None
    file_id: str | None


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
    """One `inbox` row for the user's message, in one transaction; the user's row is created on their first message.

    The message is the user's private chat and its `message_id` (DUP#1). True when this stored it; False when it was
    stored before (Telegram sent the same update again), and then nothing changes.
    """
    async with engine.begin() as conn:
        await conn.execute(pg_insert(users).values(id=user_id).on_conflict_do_nothing(index_elements=[users.c.id]))
        stored = await conn.execute(
            pg_insert(inbox)
            .values(user_id=user_id, message_id=message_id, kind=item.kind, text=item.text, file_id=item.file_id)
            .on_conflict_do_nothing(index_elements=[inbox.c.user_id, inbox.c.message_id])
            .returning(inbox.c.message_id)
        )
        return stored.first() is not None
