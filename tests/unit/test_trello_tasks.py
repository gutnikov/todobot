"""TRL#4 in the running app: with Trello connected, `/tasks` shows each task as its card is now (name, list in lower
case), hides archived and deleted cards, marks a task with no card «ещё не в Trello», and says so when Trello does not
answer; a user who has not connected sees «ещё не в Trello» for each task. CAP#2's form holds."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet
from fakes import (
    APP_SECRET_KEY,
    HEADER,
    SECRET,
    FakeBotApi,
    eventually,
    logs_on_stdout,
    message_update,
    running_app,
    trello_settings,
)
from trello_fake import FakeTrello

from todobot import inbox, texts
from todobot.crypto import SecretBox
from todobot.db import create_engine
from todobot.inbox import InboxItem
from todobot.tasks import render_entries
from todobot.trello import store
from todobot.web import WEBHOOK_PATH

ANNA = 5_812_345_678
BORIS = 402_117_993
METERS = "QK1 Сделать поверку счётчиков"
DENTIST = "QK2 Записаться к стоматологу"
TYRES = "QK3 Купить шины"
RENAMED = "QK4 Стоматолог: чистка и восьмёрки"
MARKERS = ("QK1", "QK2", "QK3", "QK4", "QK5")

Client = TestClient[Any, Any]
_message_ids = itertools.count(1)


async def record(database_url: str, user_id: int, *task_texts: str) -> None:
    """Tasks recorded as the webhook records them, but without a nudge: no round makes their cards."""
    engine = create_engine(database_url)
    try:
        for text in task_texts:
            await inbox.save(engine, user_id, next(_message_ids), InboxItem("text", text, None))
    finally:
        await engine.dispose()


async def connect(database_url: str, trello: FakeTrello, user_id: int, key: str = APP_SECRET_KEY) -> None:
    engine = create_engine(database_url)
    try:
        await store.save_connection(engine, user_id, SecretBox(key).seal(trello.issue_access().dumps()))
    finally:
        await engine.dispose()


def run_app(database_url: str, trello: FakeTrello, steps: Callable[[Client, FakeBotApi], Awaitable[Any]]) -> Any:
    """The app with Trello at `trello`; its loop runs a round as it starts (and when nudged)."""

    async def run() -> Any:
        async with (
            TestServer(trello.app) as trello_server,
            running_app(database_url, trello=trello_settings(trello_server)) as (client, api),
        ):
            return await steps(client, api)

    return asyncio.run(run())


async def ask_for_tasks(client: Client, api: FakeBotApi, user_id: int = ANNA) -> list[str]:
    since = len(api.calls)
    update = message_update(
        user_id, text="/tasks", entities=[{"type": "bot_command", "offset": 0, "length": len("/tasks")}]
    )
    response = await client.post(WEBHOOK_PATH, json=update, headers={HEADER: SECRET})
    assert response.status == 200
    return [form["text"] for method, form in api.calls[since:] if method == "sendMessage"]


def records(stdout: str, event: str) -> list[dict[str, Any]]:
    return [record for line in stdout.splitlines() if (record := json.loads(line))["event"] == event]


def test_tasks_shows_the_cards_as_they_are_now(migrated_database_url, capsys) -> None:
    trello = FakeTrello()
    asyncio.run(record(migrated_database_url, ANNA, METERS, DENTIST, TYRES))
    asyncio.run(record(migrated_database_url, BORIS, "QK5 Забрать посылку"))
    asyncio.run(connect(migrated_database_url, trello, ANNA))

    async def steps(client: Client, api: FakeBotApi) -> list[str]:
        await eventually(lambda: len(trello.cards) == 3)  # the round at start made them
        board_id = trello.board_named("todobot")["id"]
        meters, dentist, tyres = trello.cards
        meters["idList"] = trello.list_named(board_id, "Исследуется")["id"]
        dentist["name"] = RENAMED
        tyres["closed"] = True
        await record(migrated_database_url, ANNA, "QK6 Позвонить маме")  # no card yet
        return await ask_for_tasks(client, api)

    with logs_on_stdout():
        replies = run_app(migrated_database_url, trello, steps)
    stdout = capsys.readouterr().out

    assert replies == [
        f"Твои задачи:\n1. {METERS} — исследуется\n2. {RENAMED} — записана\n3. QK6 Позвонить маме — ещё не в Trello"
    ]
    [listed] = records(stdout, "tasks_listed")
    assert (listed["user_id"], listed["tasks"], listed["messages"]) == (ANNA, 3, 1)
    for marker in MARKERS:
        assert marker not in stdout


def test_a_card_in_a_list_of_the_users_own_shows_that_list(migrated_database_url) -> None:
    trello = FakeTrello()
    board_id = trello.add_board("todobot", ["Записана", "Идеи"])
    asyncio.run(record(migrated_database_url, ANNA, METERS))
    asyncio.run(connect(migrated_database_url, trello, ANNA))

    async def steps(client: Client, api: FakeBotApi) -> list[str]:
        [card] = await eventually(lambda: trello.cards)
        card["idList"] = trello.list_named(board_id, "Идеи")["id"]
        return await ask_for_tasks(client, api)

    assert run_app(migrated_database_url, trello, steps) == [f"Твои задачи:\n1. {METERS} — идеи"]


def test_a_user_who_has_not_connected_sees_their_tasks_not_in_trello_yet(migrated_database_url) -> None:
    trello = FakeTrello()
    asyncio.run(record(migrated_database_url, ANNA, METERS, DENTIST))

    async def steps(client: Client, api: FakeBotApi) -> list[str]:
        return await ask_for_tasks(client, api)

    assert run_app(migrated_database_url, trello, steps) == [
        f"Твои задачи:\n1. {METERS} — ещё не в Trello\n2. {DENTIST} — ещё не в Trello"
    ]
    assert trello.requests == []


def test_access_that_cannot_be_opened_counts_as_not_connected(migrated_database_url) -> None:
    trello = FakeTrello()
    asyncio.run(record(migrated_database_url, ANNA, METERS))
    asyncio.run(connect(migrated_database_url, trello, ANNA, key=Fernet.generate_key().decode()))

    async def steps(client: Client, api: FakeBotApi) -> list[str]:
        return await ask_for_tasks(client, api)

    assert run_app(migrated_database_url, trello, steps) == [f"Твои задачи:\n1. {METERS} — ещё не в Trello"]


def test_when_trello_does_not_answer_tasks_says_so_and_only_that(migrated_database_url, capsys) -> None:
    trello = FakeTrello()
    asyncio.run(record(migrated_database_url, ANNA, METERS))
    asyncio.run(connect(migrated_database_url, trello, ANNA))

    async def steps(client: Client, api: FakeBotApi) -> list[str]:
        await eventually(lambda: trello.cards)
        trello.refusing = True
        return await ask_for_tasks(client, api)

    with logs_on_stdout():
        replies = run_app(migrated_database_url, trello, steps)
    stdout = capsys.readouterr().out

    assert replies == [texts.TRELLO_DOWN]
    [unavailable] = records(stdout, "trello_unavailable")
    assert (unavailable["level"], unavailable["user_id"]) == ("WARNING", ANNA)
    assert unavailable["error"].endswith(": HTTP 500")
    assert not records(stdout, "tasks_listed")


def test_with_no_tasks_the_board_is_still_read(migrated_database_url) -> None:
    trello = FakeTrello()
    asyncio.run(connect(migrated_database_url, trello, ANNA))

    async def steps(client: Client, api: FakeBotApi) -> list[list[str]]:
        await eventually(lambda: len(trello.lists) == len(texts.STATUS_LISTS))
        answers = [await ask_for_tasks(client, api)]
        trello.refusing = True
        answers.append(await ask_for_tasks(client, api))
        return answers

    assert run_app(migrated_database_url, trello, steps) == [[texts.NO_TASKS], [texts.TRELLO_DOWN]]


def test_a_deleted_board_hides_its_cards_and_is_set_up_again(migrated_database_url) -> None:
    trello = FakeTrello()
    asyncio.run(record(migrated_database_url, ANNA, METERS))
    asyncio.run(connect(migrated_database_url, trello, ANNA))

    async def steps(client: Client, api: FakeBotApi) -> list[str]:
        await eventually(lambda: trello.cards)
        old = trello.board_named("todobot")["id"]
        trello.boards.clear()  # Анна deleted the board, and its lists and cards with it
        trello.lists.clear()
        trello.cards.clear()
        await record(migrated_database_url, ANNA, DENTIST)
        replies = await ask_for_tasks(client, api)
        new = await eventually(lambda: trello.boards and trello.cards and trello.board_named("todobot")["id"])
        assert new != old
        return replies

    assert run_app(migrated_database_url, trello, steps) == [f"Твои задачи:\n1. {DENTIST} — ещё не в Trello"]
    with psycopg.connect(migrated_database_url) as conn:
        cards = conn.execute("SELECT text, card_id IS NOT NULL FROM tasks ORDER BY id").fetchall()
    assert cards == [(METERS, True), (DENTIST, True)]  # the new task's card on the new board


def test_entries_are_numbered_and_labelled_as_cap_lists_them() -> None:
    assert render_entries([("Раз", "исследуется"), ("Два", "ещё не в Trello")]) == [
        "Твои задачи:\n1. Раз — исследуется\n2. Два — ещё не в Trello"
    ]
    assert render_entries([]) == [texts.NO_TASKS]
