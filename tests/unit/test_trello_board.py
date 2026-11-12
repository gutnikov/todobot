"""TRL#2: the board «todobot» and its five lists, found or made, the missing lists added in order and nothing else
changed; TRL#3: a task's card text."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from aiohttp.test_utils import TestServer
from fakes import trello_settings
from trello_fake import FakeTrello

from todobot import texts
from todobot.trello.api import TrelloApi
from todobot.trello.board import BoardSetUp, TrelloList, card_text, lists_to_add, made_at, set_up_board

STATUS = texts.STATUS_LISTS
RECORDED, RESEARCHED, WAITING, PLANNED, DONE = STATUS


def lists(*named: tuple[str, float]) -> list[TrelloList]:
    return [TrelloList(f"list-{number}", name, pos) for number, (name, pos) in enumerate(named)]


@pytest.mark.parametrize(
    ("existing", "plan"),
    [
        pytest.param([], [(name, "bottom") for name in STATUS], id="no-lists"),
        pytest.param(
            lists((RECORDED, 16384), ("Идеи", 32768)),
            [(name, "bottom") for name in STATUS[1:]],
            id="annas-board",
        ),
        pytest.param(lists(*((name, 100.0 * n) for n, name in enumerate(STATUS, 1))), [], id="all-there"),
        pytest.param(
            lists((RECORDED, 100), ("Идеи", 200), (DONE, 500)),
            [(RESEARCHED, 300.0), (WAITING, 400.0), (PLANNED, 450.0)],
            id="between-the-ones-there",
        ),
        pytest.param(
            lists((RESEARCHED, 100)),
            [(RECORDED, 50.0), (WAITING, "bottom"), (PLANNED, "bottom"), (DONE, "bottom")],
            id="the-first-before-the-next-one-there",
        ),
        pytest.param(
            lists((DONE, 100), (RECORDED, 200)),
            [(RESEARCHED, "bottom"), (WAITING, "bottom"), (PLANNED, "bottom")],
            id="out-of-order-lists-stay-so",
        ),
        pytest.param(
            lists((RECORDED, 100), (RECORDED, 50), ("Идеи", 300)),
            [(name, "bottom") for name in STATUS[1:]],
            id="a-name-twice",
        ),
    ],
)
def test_lists_to_add(existing: list[TrelloList], plan: list[tuple[str, float | str]]) -> None:
    assert lists_to_add(existing) == plan


@pytest.mark.parametrize(
    ("text", "card"),
    [
        pytest.param("Сделать поверку счётчиков", ("Сделать поверку счётчиков", ""), id="one-line"),
        pytest.param("Купить молоко\nПозвонить маме", ("Купить молоко", "Купить молоко\nПозвонить маме"), id="lines"),
        pytest.param(
            "Купить молоко\r\nПозвонить маме", ("Купить молоко", "Купить молоко\r\nПозвонить маме"), id="crlf"
        ),
        pytest.param("\n  Купить молоко  \nи хлеб", ("Купить молоко", "\n  Купить молоко  \nи хлеб"), id="blank-first"),
    ],
)
def test_card_text(text: str, card: tuple[str, str]) -> None:
    assert card_text(text) == card


def test_made_at_reads_trellos_ids() -> None:
    assert made_at("665f0a1b2c3d4e5f00000001") == datetime(2024, 6, 4, 12, 35, 39, tzinfo=UTC)
    assert made_at("not-an-id") is None


def _set_up(trello: FakeTrello, times: int = 1) -> tuple[list[BoardSetUp], list[str]]:
    access = trello.issue_access()
    noted: list[str] = []

    async def note(board_id: str) -> None:
        noted.append(board_id)

    async def run() -> list[BoardSetUp]:
        async with TestServer(trello.app) as server, TrelloApi(trello_settings(server)) as api:
            return [await set_up_board(api, access, on_board=note) for _ in range(times)]

    return asyncio.run(run()), noted


def _names(trello: FakeTrello, board_id: str) -> list[str]:
    return [x["name"] for x in trello.lists_of(board_id)]


def test_with_no_board_it_makes_one_without_trellos_lists_and_with_the_five() -> None:
    trello = FakeTrello()

    [done], noted = _set_up(trello)

    board = trello.board_named("todobot")
    assert done == BoardSetUp(board["id"], trello.list_named(board["id"], RECORDED)["id"], True, 5)
    assert noted == [board["id"]]
    assert _names(trello, board["id"]) == list(STATUS)
    [made] = trello.calls("POST", "/1/boards")
    assert made.body == {"name": "todobot", "defaultLists": False}


def test_an_existing_board_is_used_its_lists_kept_and_the_missing_ones_added() -> None:
    trello = FakeTrello()
    other = trello.add_board("Дом", ["Идеи"])
    board_id = trello.add_board("todobot", [RECORDED, "Идеи"])
    before = [dict(x) for x in trello.lists]

    [done], _ = _set_up(trello)

    assert done == BoardSetUp(board_id, before[1]["id"], False, 4)
    assert [b["id"] for b in trello.boards] == [other, board_id]  # no board made
    assert _names(trello, board_id) == [RECORDED, "Идеи", *STATUS[1:]]
    assert trello.lists[: len(before)] == before  # the lists there are as they were
    assert _names(trello, other) == ["Идеи"]
    assert not trello.calls("POST", "/1/boards")


def test_an_archived_board_is_not_used() -> None:
    trello = FakeTrello()
    archived = trello.add_board("todobot", list(STATUS), closed=True)

    [done], _ = _set_up(trello)

    assert done.board_id != archived
    assert done.created


def test_set_up_again_makes_nothing() -> None:
    trello = FakeTrello()

    first, second = _set_up(trello, times=2)[0]

    assert first.board_id == second.board_id
    assert first.recorded_list_id == second.recorded_list_id
    assert (second.created, second.lists_added) == (False, 0)
    posts: list[Any] = [r for r in trello.requests if r.method == "POST"]
    assert len(posts) == 1 + len(STATUS)
