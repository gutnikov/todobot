"""TRL in the database: the OAuth requests on their way (TRL#1), the users' connections (TRL#1, TRL#2) and the tasks'
cards (TRL#3). Secrets come in and go out sealed (`todobot.crypto`); this module never sees them in the clear."""

from __future__ import annotations

import zlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from todobot.schema import tasks, trello_connections, trello_requests, users

# The advisory lock one card sync holds (`sync_lock`): one key for the whole app.
SYNC_LOCK_KEY = zlib.crc32(b"todobot.trello.sync")


async def _ensure_user(conn: AsyncConnection, user_id: int) -> None:
    await conn.execute(pg_insert(users).values(id=user_id).on_conflict_do_nothing(index_elements=[users.c.id]))


# --- TRL#1: OAuth requests on their way -------------------------------------------------------------------------------


async def save_request(engine: AsyncEngine, *, token: str, sealed_secret: str, user_id: int, ttl: timedelta) -> None:
    """The request token `/trello` got for the user, valid for `ttl`. The user's earlier ones end here (only the
    latest link works), and so do everyone's expired ones."""
    async with engine.begin() as conn:
        await _ensure_user(conn, user_id)
        await conn.execute(
            delete(trello_requests).where(
                (trello_requests.c.user_id == user_id) | (trello_requests.c.expires_at < func.now())
            )
        )
        await conn.execute(
            trello_requests.insert().values(
                token=token, secret=sealed_secret, user_id=user_id, expires_at=func.now() + ttl
            )
        )


@dataclass(frozen=True, slots=True)
class PendingRequest:
    user_id: int
    sealed_secret: str
    expired: bool


async def take_request(engine: AsyncEngine, token: str) -> PendingRequest | None:
    """The request the callback's `oauth_token` names, taken out: it serves once. None when there is none."""
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                delete(trello_requests)
                .where(trello_requests.c.token == token)
                .returning(
                    trello_requests.c.user_id,
                    trello_requests.c.secret,
                    (trello_requests.c.expires_at <= func.now()).label("expired"),
                )
            )
        ).first()
    if row is None:
        return None
    return PendingRequest(user_id=row.user_id, sealed_secret=row.secret, expired=bool(row.expired))


# --- TRL#1, TRL#2: the users' connections -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Connection:
    user_id: int
    sealed_access: str  # also tells one connection from the next: each seal is different
    board_id: str | None
    list_id: str | None


def _connection(row: Any) -> Connection:  # a Row with the table's columns
    return Connection(user_id=row.user_id, sealed_access=row.access, board_id=row.board_id, list_id=row.list_id)


async def save_connection(engine: AsyncEngine, user_id: int, sealed_access: str) -> None:
    """The user's access to their Trello, replacing what they had; their board is to be set up (again): the access may
    be to another Trello account."""
    statement = pg_insert(trello_connections).values(user_id=user_id, access=sealed_access)
    statement = statement.on_conflict_do_update(
        index_elements=[trello_connections.c.user_id],
        set_={"access": statement.excluded.access, "list_id": None, "connected_at": func.now()},
    )
    async with engine.begin() as conn:
        await _ensure_user(conn, user_id)
        await conn.execute(statement)


async def connection_of(engine: AsyncEngine, user_id: int) -> Connection | None:
    async with engine.connect() as conn:
        row = (await conn.execute(select(trello_connections).where(trello_connections.c.user_id == user_id))).first()
    return None if row is None else _connection(row)


async def connections_to_set_up(engine: AsyncEngine) -> list[Connection]:
    """The connections whose board is not set up (yet, or again)."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(trello_connections)
            .where(trello_connections.c.list_id.is_(None))
            .order_by(trello_connections.c.connected_at)
        )
        return [_connection(row) for row in rows]


async def set_board(engine: AsyncEngine, connection: Connection, board_id: str) -> None:
    """The board found or made for `connection`; not when the user has connected anew since."""
    await _update_connection(engine, connection, board_id=board_id)


async def set_recorded_list(engine: AsyncEngine, connection: Connection, list_id: str) -> None:
    """The board is set up: cards go into the list `list_id` («Записана»)."""
    await _update_connection(engine, connection, list_id=list_id)


async def reset_board(engine: AsyncEngine, connection: Connection) -> None:
    """The board or its list is gone (Trello says so): set it up again."""
    await _update_connection(engine, connection, list_id=None)


async def _update_connection(engine: AsyncEngine, connection: Connection, **values: str | None) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(trello_connections)
            .where(
                trello_connections.c.user_id == connection.user_id,
                trello_connections.c.access == connection.sealed_access,
            )
            .values(**values)
        )


# --- TRL#3: the tasks' cards ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskWithoutCard:
    task_id: int
    text: str
    requested_at: datetime | None  # Trello was asked for its card before, with no card id back
    connection: Connection


async def tasks_without_cards(engine: AsyncEngine) -> list[TaskWithoutCard]:
    """Every task with no card whose user is connected and has the board set up, by user, oldest first."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(
                tasks.c.id,
                tasks.c.text,
                tasks.c.card_requested_at,
                trello_connections.c.user_id,
                trello_connections.c.access,
                trello_connections.c.board_id,
                trello_connections.c.list_id,
            )
            .join(trello_connections, trello_connections.c.user_id == tasks.c.user_id)
            .where(
                tasks.c.card_id.is_(None),
                trello_connections.c.board_id.is_not(None),
                trello_connections.c.list_id.is_not(None),
            )
            .order_by(tasks.c.user_id, tasks.c.created_at, tasks.c.id)
        )
        return [
            TaskWithoutCard(
                task_id=row.id, text=row.text, requested_at=row.card_requested_at, connection=_connection(row)
            )
            for row in rows
        ]


async def mark_card_requested(engine: AsyncEngine, task_id: int) -> None:
    """Before Trello is asked for the task's card: from now on, a card may exist without its id here."""
    async with engine.begin() as conn:
        await conn.execute(
            update(tasks).where(tasks.c.id == task_id, tasks.c.card_id.is_(None)).values(card_requested_at=func.now())
        )


async def set_card(engine: AsyncEngine, task_id: int, card_id: str) -> bool:
    """The task's card; False when the task has one already (or is gone), and then nothing changes."""
    async with engine.begin() as conn:
        done = await conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id, tasks.c.card_id.is_(None))
            .values(card_id=card_id)
            .returning(tasks.c.id)
        )
        return done.first() is not None


async def cards_taken(engine: AsyncEngine, card_ids: Sequence[str]) -> set[str]:
    """Which of `card_ids` are some task's card already."""
    if not card_ids:
        return set()
    async with engine.connect() as conn:
        rows = await conn.execute(select(tasks.c.card_id).where(tasks.c.card_id.in_(list(card_ids))))
        return {row.card_id for row in rows}


@asynccontextmanager
async def sync_lock(engine: AsyncEngine) -> AsyncIterator[bool]:
    """True when this holds the card sync's lock for the block, False when another process holds it now.

    A transaction-level advisory lock on a connection of its own: released when the block ends, or when the process
    dies and its connection with it. Two processes (one deploy taking over from the last) never set up a board or
    make a card at the same time.
    """
    async with engine.connect() as conn, conn.begin():
        locked = (await conn.execute(select(func.pg_try_advisory_xact_lock(SYNC_LOCK_KEY)))).scalar_one()
        yield bool(locked)
