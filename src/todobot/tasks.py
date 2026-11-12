"""Tasks (CAP): a plain text message becomes one (CAP#1), and `/tasks` lists the sender's (CAP#2); with Trello connected
the list's lines come from the board (TRL#4, `todobot.trello`), in the same form."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from todobot import schema, texts

# The status of a task just recorded, as the table keeps it; texts.STATUS_LABELS has its label, «записана».
RECORDED = "recorded"

# The most one Telegram message holds, in UTF-16 code units (Telegram counts so: an emoji outside the BMP is 2).
MESSAGE_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class Task:
    text: str
    status: str
    card_id: str | None = None  # its Trello card (TRL#3), once it is made


async def record(conn: AsyncConnection, user_id: int, message_id: int, text: str) -> None:
    """A task with status `recorded` for the user's message, on `conn`: in the transaction that stores its inbox item
    (`todobot.inbox.save`)."""
    await conn.execute(insert(schema.tasks).values(user_id=user_id, message_id=message_id, text=text, status=RECORDED))


async def of_user(engine: AsyncEngine, user_id: int) -> list[Task]:
    """The user's tasks, oldest first; nobody else's."""
    tasks = schema.tasks
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(tasks.c.text, tasks.c.status, tasks.c.card_id)
            .where(tasks.c.user_id == user_id)
            .order_by(tasks.c.created_at, tasks.c.id)
        )
        return [Task(text=row.text, status=row.status, card_id=row.card_id) for row in rows]


def render(tasks: Sequence[Task], limit: int = MESSAGE_LIMIT) -> list[str]:
    """CAP#2: the reply to /tasks, as the messages to send in order: each task's text and its status's label."""
    return render_entries([(task.text, texts.STATUS_LABELS[task.status]) for task in tasks], limit)


def render_entries(entries: Sequence[tuple[str, str]], limit: int = MESSAGE_LIMIT) -> list[str]:
    """The reply to /tasks for `entries`, (text, label) each, as the messages to send in order (CAP#2, TRL#4).

    The header and one line per entry, `<n>. <text> — <label>`, split into messages between lines; or, with no entry,
    texts.NO_TASKS alone.
    """
    if not entries:
        return [texts.NO_TASKS]
    lines = [
        texts.TASK_LINE.format(number=number, text=text, status=label)
        for number, (text, label) in enumerate(entries, 1)
    ]
    return split_messages([texts.TASKS_HEADER, *lines], limit)


def telegram_length(text: str) -> int:
    """The length Telegram counts against its limits: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def split_messages(lines: Sequence[str], limit: int = MESSAGE_LIMIT) -> list[str]:
    """`lines`, joined by line breaks, in as few messages of at most `limit` UTF-16 code units as they fit.

    A message breaks between lines, and the next one starts with the next line. A line (a task's text may hold line
    breaks of its own: it stays one line here) too long for one message gets messages of its own: `cut`.
    """
    messages: list[str] = []
    current: list[str] = []
    length = 0  # of "\n".join(current)
    for line in lines:
        line_length = telegram_length(line)
        if current and length + 1 + line_length <= limit:
            current.append(line)
            length += 1 + line_length
            continue
        if current:
            messages.append("\n".join(current))
            current, length = [], 0
        if line_length <= limit:
            current, length = [line], line_length
        else:
            messages.extend(cut(line, limit))
    if current:
        messages.append("\n".join(current))
    return messages


def cut(text: str, limit: int) -> list[str]:
    """`text` in pieces of at most `limit` UTF-16 code units that, put together, give it back.

    Each piece ends after the last space or line break that fits, so a word stays whole; mid-word only when no space
    fits. A character is never split (an emoji is two code units).
    """
    pieces: list[str] = []
    while telegram_length(text) > limit:
        end = _fitting(text, limit)
        space = max(text.rfind(" ", 0, end), text.rfind("\n", 0, end))
        if space > 0:
            end = space + 1
        pieces.append(text[:end])
        text = text[end:]
    pieces.append(text)
    return pieces


def _fitting(text: str, limit: int) -> int:
    """How many characters from the start of `text` fit in `limit` UTF-16 code units (at least one)."""
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return max(index, 1)
    return len(text)
