"""DUP#1 in the database: `inbox.save` stores a message once, keyed by the user's private chat and its message_id."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg

from todobot import inbox
from todobot.db import create_engine
from todobot.inbox import InboxItem

ANNA = 5_812_345_678
BORIS = 402_117_993
ITEM = InboxItem("text", "Забрать посылку", None)


async def _save_each(database_url: str, *saves: tuple[int, int, InboxItem]) -> list[bool]:
    """Each save in turn, as separate deliveries would make them."""
    engine = create_engine(database_url)
    try:
        return [await inbox.save(engine, user_id, message_id, item) for user_id, message_id, item in saves]
    finally:
        await engine.dispose()


async def _save_at_once(database_url: str, user_id: int, message_id: int, times: int) -> list[bool]:
    """The same message saved by `times` deliveries at the same moment, each on its own connection."""
    engine = create_engine(database_url)
    try:
        return list(await asyncio.gather(*(inbox.save(engine, user_id, message_id, ITEM) for _ in range(times))))
    finally:
        await engine.dispose()


def _inbox(database_url: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(database_url) as conn:
        return conn.execute(
            "SELECT user_id, message_id, kind, text FROM inbox ORDER BY received_at, user_id, message_id"
        ).fetchall()


def test_the_same_message_again_is_not_stored_again(migrated_database_url: str) -> None:
    saved = asyncio.run(_save_each(migrated_database_url, (BORIS, 1001, ITEM), (BORIS, 1001, ITEM)))

    assert saved == [True, False]
    assert _inbox(migrated_database_url) == [(BORIS, 1001, "text", "Забрать посылку")]


def test_a_repeat_changes_nothing_that_was_stored(migrated_database_url: str) -> None:
    other = InboxItem("photo", "Чек", "photo-x")

    saved = asyncio.run(_save_each(migrated_database_url, (BORIS, 1001, ITEM), (BORIS, 1001, other)))

    assert saved == [True, False]
    assert _inbox(migrated_database_url) == [(BORIS, 1001, "text", "Забрать посылку")]


def test_a_new_message_with_the_same_text_is_a_new_item(migrated_database_url: str) -> None:
    saved = asyncio.run(_save_each(migrated_database_url, (BORIS, 1001, ITEM), (BORIS, 1002, ITEM)))

    assert saved == [True, True]
    assert _inbox(migrated_database_url) == [
        (BORIS, 1001, "text", "Забрать посылку"),
        (BORIS, 1002, "text", "Забрать посылку"),
    ]


def test_the_same_message_id_in_another_users_chat_is_another_message(migrated_database_url: str) -> None:
    # message_id counts per chat: two users' chats have their own message 1001.
    saved = asyncio.run(_save_each(migrated_database_url, (ANNA, 1001, ITEM), (BORIS, 1001, ITEM)))

    assert saved == [True, True]
    assert sorted(row[0] for row in _inbox(migrated_database_url)) == sorted([ANNA, BORIS])


def test_deliveries_of_one_message_at_the_same_moment_store_it_once(migrated_database_url: str) -> None:
    saved = asyncio.run(_save_at_once(migrated_database_url, ANNA, 1001, times=3))

    assert sorted(saved) == [False, False, True]
    assert _inbox(migrated_database_url) == [(ANNA, 1001, "text", "Забрать посылку")]
