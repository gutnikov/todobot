"""CAP: which inbox item becomes a task, recording it with its item (one transaction, once per message), the sender's
tasks oldest first, and the list as Telegram messages of at most 4096 UTF-16 code units."""

from __future__ import annotations

import asyncio
import itertools
from typing import Any

import psycopg
import pytest
from sqlalchemy.exc import IntegrityError

from todobot import inbox, tasks, texts
from todobot.db import create_engine
from todobot.inbox import InboxItem
from todobot.tasks import MESSAGE_LIMIT, Task, cut, render, split_messages, telegram_length

ANNA = 5_812_345_678
BORIS = 402_117_993

# --- Which item becomes a task (CAP#1) -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "task_text"),
    [
        pytest.param(InboxItem("text", "Купить молоко", None), "Купить молоко", id="plain-text"),
        pytest.param(
            InboxItem("text", "Купить молоко\nПозвонить маме", None), "Купить молоко\nПозвонить маме", id="several"
        ),
        pytest.param(InboxItem("text", "Вынести мусор / пластик", None), "Вынести мусор / пластик", id="slash-inside"),
        pytest.param(InboxItem("text", "/help", None), None, id="command"),
        pytest.param(InboxItem("text", "/tasks", None), None, id="tasks-command"),
        # CAP's seams: a text that starts with / is a command, with Telegram's bot_command entity or without it
        pytest.param(InboxItem("text", "/home/anna/список", None), None, id="starts-with-slash"),
        pytest.param(InboxItem("voice", None, "voice-1"), None, id="voice"),
        pytest.param(InboxItem("voice", "Позвонить маме", "voice-1"), None, id="voice-with-caption"),
        pytest.param(InboxItem("photo", "Чек из аптеки", "photo-x"), None, id="photo-with-caption"),
        pytest.param(InboxItem("other", "Список", None), None, id="document-with-caption"),
        pytest.param(InboxItem("other", None, None), None, id="sticker"),
    ],
)
def test_only_a_plain_text_becomes_a_task(item: InboxItem, task_text: str | None) -> None:
    assert item.task_text == task_text


# --- The list's text (CAP#2) -----------------------------------------------------------------------------------------


def recorded(*texts_: str) -> list[Task]:
    return [Task(text, tasks.RECORDED) for text in texts_]


def test_no_tasks() -> None:
    assert render([]) == ["Задач пока нет."]


def test_the_list_exactly() -> None:
    listed = recorded("Сделать поверку счётчиков", "Записаться к стоматологу")

    assert render(listed) == [
        "Твои задачи:\n1. Сделать поверку счётчиков — записана\n2. Записаться к стоматологу — записана"
    ]


def test_a_task_with_line_breaks_is_one_numbered_entry() -> None:
    assert render(recorded("Купить молоко\nПозвонить маме", "Вынести мусор")) == [
        "Твои задачи:\n1. Купить молоко\nПозвонить маме — записана\n2. Вынести мусор — записана"
    ]


def test_every_status_has_its_label() -> None:
    # The table allows these statuses (its CHECK); the list must have a label for each.
    assert set(texts.STATUS_LABELS) == {tasks.RECORDED}


# --- Splitting into messages -----------------------------------------------------------------------------------------


def test_telegram_counts_utf16_code_units() -> None:
    assert telegram_length("задача") == 6
    assert telegram_length("✅") == 1  # in the BMP
    assert telegram_length("🥛") == 2  # outside it: a surrogate pair


@pytest.mark.parametrize(
    ("lines", "limit", "messages"),
    [
        pytest.param(["aaaa", "bbbb"], 9, ["aaaa\nbbbb"], id="exactly-at-the-limit"),
        pytest.param(["aaaa", "bbbbb"], 9, ["aaaa", "bbbbb"], id="one-over-goes-on"),
        pytest.param(["aa", "bb", "cc", "dd"], 5, ["aa\nbb", "cc\ndd"], id="greedy"),
        pytest.param(["aaaa", "bbbb"], 4, ["aaaa", "bbbb"], id="a-line-exactly-the-limit"),
        # 🥛 is two code units: "🥛🥛\nb" is 6 of them, though Python counts 4 characters
        pytest.param(["🥛🥛", "b"], 5, ["🥛🥛", "b"], id="utf16-not-characters"),
    ],
)
def test_split_between_lines(lines: list[str], limit: int, messages: list[str]) -> None:
    assert split_messages(lines, limit) == messages


def test_a_long_list_comes_whole_and_in_order_in_several_messages() -> None:
    listed = recorded(*(f"Задача {number:02d}: " + "сделать дело, " * 21 for number in range(1, 41)))
    lines = ["Твои задачи:", *(f"{number}. {task.text} — записана" for number, task in enumerate(listed, 1))]

    messages = render(listed)

    assert len(messages) > 1
    assert all(telegram_length(message) <= MESSAGE_LIMIT for message in messages)
    assert [line for message in messages for line in message.split("\n")] == lines
    # as full as they can be: the next message's first line did not fit into the one before
    for before, after in itertools.pairwise(messages):
        assert telegram_length(before) + 1 + telegram_length(after.split("\n")[0]) > MESSAGE_LIMIT


def test_a_task_longer_than_a_message_gets_messages_of_its_own() -> None:
    """The header goes first on its own; the long line is cut after a space, so no word is broken, into pieces that
    together give it back; the next task starts a new message."""
    word = "молоко "
    long_text = (word * (MESSAGE_LIMIT // len(word) + 50)).strip()
    listed = recorded(long_text, "Вынести мусор")
    long_line = f"1. {long_text} — записана"

    messages = render(listed)

    assert messages[0] == "Твои задачи:"
    assert messages[-1] == "2. Вынести мусор — записана"
    pieces = messages[1:-1]
    assert len(pieces) == 2
    assert "".join(pieces) == long_line
    assert all(telegram_length(piece) <= MESSAGE_LIMIT for piece in pieces)
    assert pieces[0].endswith("молоко ")
    assert pieces[1].startswith("молоко")


def test_a_long_line_without_spaces_is_cut_at_the_limit() -> None:
    line = "я" * 10

    assert cut(line, 4) == ["яяяя", "яяяя", "яя"]


def test_a_cut_never_splits_a_character() -> None:
    # Each 🥛 is two code units: at a limit of 5, two of them fit, a third would split.
    assert cut("🥛🥛🥛🥛", 5) == ["🥛🥛", "🥛🥛"]


def test_a_cut_breaks_after_a_line_break_too() -> None:
    assert cut("ab\ncdef gh", 6) == ["ab\n", "cdef ", "gh"]


def test_a_line_that_fits_is_not_cut() -> None:
    assert cut("ab cd", 5) == ["ab cd"]


# --- Recording with the inbox item, once (CAP#1, DUP#1) ---------------------------------------------------------------


async def _save_each(database_url: str, *saves: tuple[int, int, InboxItem]) -> list[bool]:
    engine = create_engine(database_url)
    try:
        return [await inbox.save(engine, user_id, message_id, item) for user_id, message_id, item in saves]
    finally:
        await engine.dispose()


async def _save_at_once(database_url: str, item: InboxItem, times: int) -> list[bool]:
    engine = create_engine(database_url)
    try:
        return list(await asyncio.gather(*(inbox.save(engine, ANNA, 1001, item) for _ in range(times))))
    finally:
        await engine.dispose()


def _tasks(database_url: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(database_url) as conn:
        return conn.execute("SELECT user_id, message_id, text, status FROM tasks ORDER BY created_at, id").fetchall()


def _inbox(database_url: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(database_url) as conn:
        return conn.execute("SELECT user_id, message_id, kind, text FROM inbox ORDER BY received_at").fetchall()


TEXT = InboxItem("text", "Забрать посылку", None)


def test_a_plain_text_is_recorded_as_a_task_tied_to_its_inbox_item(migrated_database_url: str) -> None:
    assert asyncio.run(_save_each(migrated_database_url, (BORIS, 1001, TEXT))) == [True]

    assert _inbox(migrated_database_url) == [(BORIS, 1001, "text", "Забрать посылку")]
    assert _tasks(migrated_database_url) == [(BORIS, 1001, "Забрать посылку", "recorded")]


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(InboxItem("text", "/help", None), id="command"),
        pytest.param(InboxItem("voice", "Позвонить маме", "voice-1"), id="voice"),
        pytest.param(InboxItem("photo", "Чек", "photo-x"), id="photo"),
        pytest.param(InboxItem("other", None, None), id="other"),
    ],
)
def test_anything_else_is_an_inbox_item_only(migrated_database_url: str, item: InboxItem) -> None:
    assert asyncio.run(_save_each(migrated_database_url, (BORIS, 1001, item))) == [True]

    assert len(_inbox(migrated_database_url)) == 1
    assert _tasks(migrated_database_url) == []


def test_the_same_message_again_makes_no_second_task(migrated_database_url: str) -> None:
    saved = asyncio.run(_save_each(migrated_database_url, (BORIS, 1001, TEXT), (BORIS, 1001, TEXT)))

    assert saved == [True, False]
    assert _tasks(migrated_database_url) == [(BORIS, 1001, "Забрать посылку", "recorded")]


def test_deliveries_of_one_message_at_the_same_moment_make_one_task(migrated_database_url: str) -> None:
    saved = asyncio.run(_save_at_once(migrated_database_url, TEXT, times=3))

    assert sorted(saved) == [False, False, True]
    assert _tasks(migrated_database_url) == [(ANNA, 1001, "Забрать посылку", "recorded")]


def test_a_new_message_with_the_same_text_is_a_new_task(migrated_database_url: str) -> None:
    asyncio.run(_save_each(migrated_database_url, (BORIS, 1001, TEXT), (BORIS, 1002, TEXT)))

    assert [row[1] for row in _tasks(migrated_database_url)] == [1001, 1002]


def test_the_item_and_its_task_are_stored_together_or_not_at_all(migrated_database_url: str) -> None:
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE tasks ADD COLUMN must_have integer NOT NULL")  # the task's INSERT now fails

    with pytest.raises(IntegrityError):
        asyncio.run(_save_each(migrated_database_url, (BORIS, 1001, TEXT)))

    assert _inbox(migrated_database_url) == []


def test_a_task_needs_its_inbox_item(migrated_database_url: str) -> None:
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s)", (BORIS,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO tasks (user_id, message_id, text, status) VALUES (%s, 1001, 'x', 'recorded')", (BORIS,)
            )


# --- The sender's tasks, oldest first (CAP#2) ------------------------------------------------------------------------


async def _of_user(database_url: str, user_id: int) -> list[Task]:
    engine = create_engine(database_url)
    try:
        return await tasks.of_user(engine, user_id)
    finally:
        await engine.dispose()


def test_only_the_users_tasks_oldest_first(migrated_database_url: str) -> None:
    asyncio.run(
        _save_each(
            migrated_database_url,
            (ANNA, 1, InboxItem("text", "первая", None)),
            (BORIS, 1, InboxItem("text", "чужая", None)),
            (ANNA, 2, InboxItem("text", "вторая", None)),
            (ANNA, 3, InboxItem("text", "третья", None)),
        )
    )
    # Oldest by created_at, whatever order the rows were inserted in.
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        conn.execute("UPDATE tasks SET created_at = now() - interval '1 day' WHERE text = 'третья'")

    assert asyncio.run(_of_user(migrated_database_url, ANNA)) == recorded("третья", "первая", "вторая")
    assert asyncio.run(_of_user(migrated_database_url, BORIS)) == recorded("чужая")
    assert asyncio.run(_of_user(migrated_database_url, 777)) == []
