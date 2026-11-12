"""TRL#2 and TRL#3 outside the webhook: the loop sets the board up and makes one card per task in «Записана», tries
again while Trello refuses, and never makes a second card: not after a lost answer or a crash between the card made and
its id stored, not when two rounds (two processes) run at once."""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from aiohttp.test_utils import TestServer
from cryptography.fernet import Fernet
from fakes import APP_SECRET_KEY, eventually, logs_on_stdout, trello_settings
from sqlalchemy.ext.asyncio import AsyncEngine
from trello_fake import FakeTrello, trello_id

from todobot import inbox, texts
from todobot.crypto import SecretBox
from todobot.db import create_engine
from todobot.inbox import InboxItem
from todobot.trello import board, store
from todobot.trello.api import Access, TrelloApi
from todobot.trello.sync import CardSync

ANNA = 5_812_345_678
BORIS = 402_117_993
RECORDED = texts.STATUS_LISTS[0]
METERS = "QM1 Сделать поверку счётчиков"  # the ASCII marker: JSON lines escape Cyrillic
MILK = "QM2 Купить молоко\nПозвонить маме"
MARKERS = ("QM1", "QM2")

_message_ids = itertools.count(1)


@dataclass
class World:
    engine: AsyncEngine
    trello: FakeTrello
    api: TrelloApi
    box: SecretBox
    sync: CardSync

    async def record(self, user_id: int, *task_texts: str) -> None:
        for text in task_texts:
            await inbox.save(self.engine, user_id, next(_message_ids), InboxItem("text", text, None))

    async def connect(self, user_id: int, box: SecretBox | None = None) -> Access:
        access = self.trello.issue_access()
        await store.save_connection(self.engine, user_id, (box or self.box).seal(access.dumps()))
        return access

    def another_sync(self) -> CardSync:
        """A second process's loop on the same database."""
        return CardSync(self.engine, self.api, self.box, retry_seconds=3600)

    def recorded_list(self) -> dict[str, Any]:
        return self.trello.list_named(self.trello.board_named(texts.BOARD_NAME)["id"], RECORDED)

    def card_posts(self) -> int:
        return len(self.trello.calls("POST", "/1/cards"))


@asynccontextmanager
async def world(database_url: str, trello: FakeTrello | None = None) -> AsyncIterator[World]:
    trello = trello or FakeTrello()
    engine = create_engine(database_url)
    try:
        async with TestServer(trello.app) as server, TrelloApi(trello_settings(server)) as api:
            box = SecretBox(APP_SECRET_KEY)
            yield World(engine, trello, api, box, CardSync(engine, api, box, retry_seconds=3600))
    finally:
        await engine.dispose()


def task_rows(database_url: str) -> list[tuple[Any, ...]]:
    """(user_id, text, card_id, card_requested_at) of every task, oldest first."""
    with psycopg.connect(database_url) as conn:
        return conn.execute(
            "SELECT user_id, text, card_id, card_requested_at FROM tasks ORDER BY created_at, id"
        ).fetchall()


def records(stdout: str, event: str) -> list[dict[str, Any]]:
    return [record for line in stdout.splitlines() if (record := json.loads(line))["event"] == event]


def test_each_task_becomes_one_card_in_recorded_and_a_second_round_makes_none(migrated_database_url, capsys) -> None:
    async def scenario() -> FakeTrello:
        async with world(migrated_database_url) as w:
            await w.record(ANNA, METERS, MILK)
            await w.connect(ANNA)
            await w.sync.run_once()
            await w.sync.run_once()
            assert w.card_posts() == 2
            return w.trello

    with logs_on_stdout():
        trello = asyncio.run(scenario())
    stdout = capsys.readouterr().out

    recorded = trello.list_named(trello.board_named("todobot")["id"], RECORDED)
    assert [(c["name"], c["desc"], c["idList"]) for c in trello.cards] == [
        (METERS, "", recorded["id"]),
        ("QM2 Купить молоко", MILK, recorded["id"]),
    ]
    assert [(row[1], row[2]) for row in task_rows(migrated_database_url)] == [
        (METERS, trello.cards[0]["id"]),
        (MILK, trello.cards[1]["id"]),
    ]
    created = records(stdout, "card_created")
    assert [(r["level"], r["user_id"]) for r in created] == [("INFO", ANNA), ("INFO", ANNA)]
    assert all(isinstance(r["task_id"], int) for r in created)
    [ready] = records(stdout, "board_ready")
    assert (ready["user_id"], ready["board_created"], ready["lists_added"]) == (ANNA, True, 5)
    for marker in MARKERS:
        assert marker not in stdout


def test_only_connected_users_tasks_get_cards(migrated_database_url) -> None:
    async def scenario() -> FakeTrello:
        async with world(migrated_database_url) as w:
            await w.record(ANNA, METERS)
            await w.record(BORIS, "Забрать посылку")
            await w.connect(ANNA)
            await w.sync.run_once()
            return w.trello

    trello = asyncio.run(scenario())

    assert [c["name"] for c in trello.cards] == [METERS]
    assert [(row[0], row[2] is None) for row in task_rows(migrated_database_url)] == [(ANNA, False), (BORIS, True)]


def test_while_trello_refuses_the_task_waits_and_gets_its_card_once_trello_takes(migrated_database_url, capsys) -> None:
    async def scenario() -> FakeTrello:
        async with world(migrated_database_url) as w:
            await w.connect(ANNA)
            await w.sync.run_once()  # the board
            w.trello.refusing = True
            await w.record(ANNA, METERS)
            await w.sync.run_once()
            await w.sync.run_once()
            assert w.trello.cards == []
            [(_, _, card_id, requested_at)] = task_rows(migrated_database_url)
            assert (card_id, isinstance(requested_at, datetime)) == (None, True)
            w.trello.refusing = False
            await w.sync.run_once()
            await w.sync.run_once()
            return w.trello

    with logs_on_stdout():
        trello = asyncio.run(scenario())
    stdout = capsys.readouterr().out

    assert [c["name"] for c in trello.cards] == [METERS]
    assert task_rows(migrated_database_url)[0][2] == trello.cards[0]["id"]
    board_id = trello.board_named("todobot")["id"]
    # The first try asks for the card; the next one first looks for a card the first may have made.
    assert [(r["level"], r["error"]) for r in records(stdout, "card_failed")] == [
        ("WARNING", "POST /1/cards: HTTP 500"),
        ("WARNING", f"GET /1/boards/{board_id}/cards/all: HTTP 500"),
    ]
    assert len(records(stdout, "card_created")) == 1
    assert "QM1" not in stdout


def test_a_card_whose_answer_was_lost_is_found_on_the_board_not_made_again(migrated_database_url, capsys) -> None:
    async def scenario() -> FakeTrello:
        async with world(migrated_database_url) as w:
            await w.connect(ANNA)
            await w.record(ANNA, METERS)
            w.trello.lose_card_answers = 1  # Trello makes the card, and the answer never comes back
            await w.sync.run_once()
            assert len(w.trello.cards) == 1
            assert task_rows(migrated_database_url)[0][2] is None
            await w.sync.run_once()
            await w.sync.run_once()
            assert w.card_posts() == 1
            return w.trello

    with logs_on_stdout():
        trello = asyncio.run(scenario())
    stdout = capsys.readouterr().out

    [card] = trello.cards
    assert task_rows(migrated_database_url)[0][2] == card["id"]
    assert len(records(stdout, "card_found")) == 1
    assert not records(stdout, "card_created")


def test_a_crash_between_the_card_made_and_its_id_stored_makes_no_second_card(migrated_database_url) -> None:
    async def scenario() -> FakeTrello:
        async with world(migrated_database_url) as w:
            await w.connect(ANNA)
            await w.sync.run_once()
            await w.record(ANNA, MILK)
            # What a round does until the process dies: the mark, then the card; its id is never stored.
            [waiting] = await store.tasks_without_cards(w.engine)
            access = Access.loads(w.box.open(waiting.connection.sealed_access) or "")
            assert access is not None
            await store.mark_card_requested(w.engine, waiting.task_id)
            await board.make_card(w.api, access, w.recorded_list()["id"], MILK)
            # The next process's round:
            await w.another_sync().run_once()
            return w.trello

    trello = asyncio.run(scenario())

    [card] = trello.cards
    assert task_rows(migrated_database_url)[0][2] == card["id"]


def test_a_card_of_the_same_name_made_long_before_is_not_taken(migrated_database_url) -> None:
    async def scenario() -> tuple[FakeTrello, str]:
        async with world(migrated_database_url) as w:
            await w.connect(ANNA)
            await w.sync.run_once()
            recorded = w.recorded_list()
            by_hand = trello_id(time.time() - 3600)  # Анна made it herself an hour ago
            w.trello.cards.append(
                {"id": by_hand, "idBoard": recorded["idBoard"], "idList": recorded["id"], "name": METERS}
                | {"desc": "", "closed": False}
            )
            await w.record(ANNA, METERS)
            [waiting] = await store.tasks_without_cards(w.engine)
            await store.mark_card_requested(w.engine, waiting.task_id)  # a request whose outcome is unknown
            await w.sync.run_once()
            return w.trello, by_hand

    trello, by_hand = asyncio.run(scenario())

    assert len(trello.cards) == 2
    assert task_rows(migrated_database_url)[0][2] not in (None, by_hand)


def test_a_card_that_is_another_tasks_is_not_taken(migrated_database_url) -> None:
    async def scenario() -> FakeTrello:
        async with world(migrated_database_url) as w:
            await w.connect(ANNA)
            await w.record(ANNA, METERS)
            await w.sync.run_once()
            await w.record(ANNA, METERS)  # the same text again, a new task
            second = (await store.tasks_without_cards(w.engine))[0]
            await store.mark_card_requested(w.engine, second.task_id)
            await w.sync.run_once()
            return w.trello

    trello = asyncio.run(scenario())

    assert len(trello.cards) == 2
    assert [row[2] for row in task_rows(migrated_database_url)] == [c["id"] for c in trello.cards]


def test_rounds_at_the_same_time_make_one_card_per_task(migrated_database_url) -> None:
    async def scenario() -> FakeTrello:
        async with world(migrated_database_url) as w:
            await w.connect(ANNA)
            await w.record(ANNA, "Раз", "Два", "Три")
            await asyncio.gather(w.sync.run_once(), w.another_sync().run_once(), w.sync.run_once())
            await asyncio.gather(w.sync.run_once(), w.another_sync().run_once())
            return w.trello

    trello = asyncio.run(scenario())

    assert len(trello.boards) == 1
    assert sorted(c["name"] for c in trello.cards) == ["Два", "Раз", "Три"]


def test_a_round_does_nothing_while_another_process_runs_one(migrated_database_url) -> None:
    async def scenario() -> FakeTrello:
        async with world(migrated_database_url) as w:
            await w.connect(ANNA)
            await w.record(ANNA, METERS)
            async with store.sync_lock(w.engine) as locked:
                assert locked
                await w.sync.run_once()
            assert w.trello.requests == []
            await w.sync.run_once()  # the lock is free again
            return w.trello

    trello = asyncio.run(scenario())

    assert [c["name"] for c in trello.cards] == [METERS]


def test_a_list_gone_from_the_board_is_set_up_again(migrated_database_url, capsys) -> None:
    async def scenario() -> FakeTrello:
        async with world(migrated_database_url) as w:
            await w.connect(ANNA)
            await w.sync.run_once()
            gone = w.recorded_list()
            w.trello.lists.remove(gone)  # Анна deleted the list «Записана»
            await w.record(ANNA, METERS)
            await w.sync.run_once()  # Trello says there is no such list
            assert w.trello.cards == []
            await w.sync.run_once()  # the board set up again, the card made
            return w.trello

    with logs_on_stdout():
        trello = asyncio.run(scenario())
    stdout = capsys.readouterr().out

    board_id = trello.board_named("todobot")["id"]
    [card] = trello.cards
    assert card["idList"] == trello.list_named(board_id, RECORDED)["id"]
    assert [r["error"] for r in records(stdout, "card_failed")] == ["POST /1/cards: HTTP 400"]
    assert len(trello.boards) == 1


def test_access_sealed_with_another_key_is_skipped(migrated_database_url, capsys) -> None:
    async def scenario() -> FakeTrello:
        async with world(migrated_database_url) as w:
            await w.connect(ANNA, box=SecretBox(Fernet.generate_key().decode()))
            await w.record(ANNA, METERS)
            await w.sync.run_once()
            return w.trello

    with logs_on_stdout():
        trello = asyncio.run(scenario())
    stdout = capsys.readouterr().out

    assert trello.requests == []
    [unreadable] = records(stdout, "trello_access_unreadable")
    assert (unreadable["level"], unreadable["user_id"]) == ("WARNING", ANNA)


def test_the_loop_makes_the_card_when_nudged_and_stops(migrated_database_url) -> None:
    async def scenario() -> FakeTrello:
        async with world(migrated_database_url) as w:
            w.sync.start()
            try:
                await w.connect(ANNA)
                await w.record(ANNA, METERS)
                w.sync.nudge()
                await eventually(lambda: w.trello.cards)
            finally:
                await w.sync.stop()
            return w.trello

    trello = asyncio.run(scenario())

    assert [c["name"] for c in trello.cards] == [METERS]


def test_a_round_that_fails_is_logged_with_its_type_only_and_the_loop_goes_on(migrated_database_url, capsys) -> None:
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE trello_connections RENAME COLUMN list_id TO list_gone")

    async def scenario() -> None:
        async with world(migrated_database_url) as w:
            w.sync.retry_seconds = 0.05
            w.sync.start()
            try:
                await asyncio.sleep(0.5)
            finally:
                await w.sync.stop()

    with logs_on_stdout():
        asyncio.run(scenario())
    stdout = capsys.readouterr().out

    failed = records(stdout, "trello_sync_failed")
    assert len(failed) > 1  # it went on
    assert {(r["level"], r["error"]) for r in failed} == {("ERROR", "ProgrammingError")}
    assert all("exc" not in r for r in failed)
