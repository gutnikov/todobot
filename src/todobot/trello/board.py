"""What the bot does on a user's Trello board: sets it up (TRL#2), makes a task's card or finds the one an earlier try
made (TRL#3), and reads it for /tasks (TRL#4). Trello calls only; the database is the caller's (`sync`,
`TrelloService`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from todobot import texts
from todobot.trello.api import Access, TrelloApi, TrelloError

# Where Trello puts a list or card asked to go last.
BOTTOM = "bottom"


def _str(value: Any, call: str) -> str:
    if not isinstance(value, str):
        raise TrelloError(call, error="UnexpectedAnswer")
    return value


def _objects(value: Any, call: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TrelloError(call, error="UnexpectedAnswer")
    return value


# --- TRL#2: the board «todobot» and its five lists --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrelloList:
    id: str
    name: str
    pos: float


def lists_to_add(existing: Sequence[TrelloList]) -> list[tuple[str, float | str]]:
    """The status lists (texts.STATUS_LISTS) the board lacks, each with the position to add it at, in the order to add
    them. The lists it has stay where they are (TRL#2: nothing else on the board is changed).

    A missing list goes right after the status list before it and before the next status list the board has, so the
    five come in their order; with no such next list, at the bottom. Lists added one after another at the bottom keep
    their order. When the board has its status lists in another order, that order is kept: moving them is not ours.
    """
    first: dict[str, TrelloList] = {}
    for trello_list in sorted(existing, key=lambda x: x.pos):
        first.setdefault(trello_list.name, trello_list)
    plan: list[tuple[str, float | str]] = []
    previous: float | None = 0.0  # the position of the status list before; None once lists go to the bottom
    for index, name in enumerate(texts.STATUS_LISTS):
        if name in first:
            pos = first[name].pos
            previous = None if previous is None else max(previous, pos)
            continue
        later = [
            first[after].pos
            for after in texts.STATUS_LISTS[index + 1 :]
            if after in first and previous is not None and first[after].pos > previous
        ]
        if later and previous is not None:
            middle = (previous + min(later)) / 2
            plan.append((name, middle))
            previous = middle
        else:
            plan.append((name, BOTTOM))
            previous = None
    return plan


@dataclass(frozen=True, slots=True)
class BoardSetUp:
    board_id: str
    recorded_list_id: str  # «Записана», where cards go
    created: bool  # the board was made now (there was none)
    lists_added: int


async def set_up_board(
    api: TrelloApi, access: Access, *, on_board: Callable[[str], Awaitable[None]] | None = None
) -> BoardSetUp:
    """TRL#2: the user's open board «todobot», made (without Trello's own lists) when there is none, with the five
    status lists, the missing ones added. Done again, it finds what it made: nothing is made twice.

    `on_board` gets the board's id as soon as it is known, before the lists are seen to.
    """
    call = "GET /1/members/me/boards"
    boards = _objects(await api.get(access, "/1/members/me/boards", {"filter": "open", "fields": "name"}), call)
    board_id = next((_str(b.get("id"), call) for b in boards if b.get("name") == texts.BOARD_NAME), None)
    created = board_id is None
    if board_id is None:
        made = await api.post(access, "/1/boards", {"name": texts.BOARD_NAME, "defaultLists": False})
        board_id = _str(made.get("id") if isinstance(made, dict) else None, "POST /1/boards")
    if on_board is not None:
        await on_board(board_id)

    call = f"GET /1/boards/{board_id}/lists"
    found = _objects(
        await api.get(access, f"/1/boards/{board_id}/lists", {"filter": "open", "fields": "name,pos"}), call
    )
    try:
        existing = [TrelloList(_str(x.get("id"), call), _str(x.get("name"), call), float(x["pos"])) for x in found]
    except (KeyError, TypeError, ValueError):
        raise TrelloError(call, error="UnexpectedAnswer") from None
    added: dict[str, str] = {}
    for name, pos in lists_to_add(existing):
        made = await api.post(access, "/1/lists", {"name": name, "idBoard": board_id, "pos": pos})
        added[name] = _str(made.get("id") if isinstance(made, dict) else None, "POST /1/lists")

    recorded = texts.STATUS_LISTS[0]
    have = sorted((x for x in existing if x.name == recorded), key=lambda x: x.pos)
    recorded_list_id = have[0].id if have else added[recorded]
    return BoardSetUp(board_id, recorded_list_id, created, len(added))


# --- TRL#3: a task's card ---------------------------------------------------------------------------------------------


def card_text(text: str) -> tuple[str, str]:
    """A task's card: its name and description. A one-line text is the name, with no description; a text of several
    lines gives its first line as the name and the whole text as the description (its first block)."""
    lines = text.splitlines()
    if len(lines) <= 1:
        return text, ""
    name = next((line.strip() for line in lines if line.strip()), text.strip())
    return name, text


async def make_card(api: TrelloApi, access: Access, list_id: str, text: str) -> str:
    """A new card for the task's `text` at the bottom of the list `list_id`; its id."""
    name, desc = card_text(text)
    made = await api.post(access, "/1/cards", {"idList": list_id, "name": name, "desc": desc, "pos": BOTTOM})
    return _str(made.get("id") if isinstance(made, dict) else None, "POST /1/cards")


def made_at(card_id: str) -> datetime | None:
    """When Trello made the card: a Trello id starts with the Unix time of its making, in 8 hex digits."""
    try:
        return datetime.fromtimestamp(int(card_id[:8], 16), UTC)
    except (ValueError, OverflowError, OSError):
        return None


async def find_card(
    api: TrelloApi,
    access: Access,
    board_id: str,
    text: str,
    *,
    made_since: datetime,
    taken: Callable[[list[str]], Awaitable[set[str]]],
) -> str | None:
    """The card an earlier request for the task's `text` may have made though its answer never came back (a crash, a
    lost connection): on the board, archived or not, named as the task's card is, made no earlier than `made_since`,
    and no task's card yet (`taken` says which of the ids are). The oldest such card, or None."""
    name = card_text(text)[0].strip()
    call = f"GET /1/boards/{board_id}/cards/all"
    cards = _objects(await api.get(access, f"/1/boards/{board_id}/cards/all", {"fields": "name"}), call)
    candidates = sorted(
        card_id
        for card in cards
        if isinstance(card_id := card.get("id"), str)
        and isinstance(card.get("name"), str)
        and card["name"].strip() == name
        and (when := made_at(card_id)) is not None
        and when >= made_since
    )
    if not candidates:
        return None
    linked = await taken(candidates)
    return next((card_id for card_id in candidates if card_id not in linked), None)


# --- TRL#4: the board as /tasks shows it ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardView:
    name: str
    list_name: str


async def read_cards(api: TrelloApi, access: Access, board_id: str) -> dict[str, CardView]:
    """The board's open cards (an archived or deleted card is not among them), by id: each its current name and the
    name of the list it is in."""
    call = f"GET /1/boards/{board_id}"
    params = {"fields": "name", "lists": "all", "list_fields": "name", "cards": "open", "card_fields": "name,idList"}
    board = await api.get(access, f"/1/boards/{board_id}", params)
    if not isinstance(board, dict):
        raise TrelloError(call, error="UnexpectedAnswer")
    names = {_str(x.get("id"), call): _str(x.get("name"), call) for x in _objects(board.get("lists", []), call)}
    return {
        _str(card.get("id"), call): CardView(_str(card.get("name"), call), names.get(str(card.get("idList")), ""))
        for card in _objects(board.get("cards", []), call)
        if not card.get("closed")
    }
