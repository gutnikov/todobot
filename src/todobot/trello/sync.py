"""TRL#2 and TRL#3 outside the webhook: a loop in `serve` that sets up the board of every user who connected Trello and
makes a card for every task that has none, every TRELLO_RETRY_SECONDS and at once when there is something new
(`nudge`: a task recorded, Trello connected). A task whose card Trello does not take waits for the next round.

No task gets a second card:
- a task's card id is stored, and only a task without one is looked at;
- before Trello is asked for a card, the task is marked (`card_requested_at`, committed). A request whose answer never
  came back (the process died, the connection broke, Trello answered 500 after making it) leaves the mark without a
  card id; the next round first looks on the board for the card it may have made (`board.find_card`) and takes that;
- one process at a time runs a round (`store.sync_lock`), so two never ask for the same card.
What is left: a card the user renamed, moved to another board or deleted before the next round finds it, or a card
made more than CLOCK_SLACK before the mark by Trello's clock, is not found, and the task gets a new one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from todobot.crypto import SecretBox
from todobot.logs import log_event
from todobot.trello import board, store
from todobot.trello.api import Access, TrelloApi, TrelloError

logger = logging.getLogger(__name__)

# How much earlier than the mark, by Trello's clock, the card of a request may have been made.
CLOCK_SLACK = timedelta(minutes=10)
# Trello's answers that mean the board or its list is not there any more: set the board up again.
GONE = frozenset({400, 404})


def open_access(box: SecretBox, connection: store.Connection) -> Access | None:
    """The connection's access in the clear; None (and a WARNING) when it cannot be opened: APP_SECRET_KEY changed
    since it was sealed. The user connects again with /trello."""
    raw = box.open(connection.sealed_access)
    access = None if raw is None else Access.loads(raw)
    if access is None:
        log_event(logger, "trello_access_unreadable", level=logging.WARNING, user_id=connection.user_id)
    return access


class CardSync:
    def __init__(self, engine: AsyncEngine, api: TrelloApi, box: SecretBox, *, retry_seconds: float) -> None:
        self.engine = engine
        self.api = api
        self.box = box
        self.retry_seconds = retry_seconds
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def nudge(self) -> None:
        """Run a round now rather than at the next tick."""
        self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="trello-sync")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.run_once()
            except Exception as exc:  # the type only: a database error's message can quote a task
                log_event(logger, "trello_sync_failed", level=logging.ERROR, error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.retry_seconds):
                    await self._wake.wait()

    async def run_once(self) -> None:
        """One round: boards to set up, then cards to make. Nothing when another process runs one now."""
        async with store.sync_lock(self.engine) as locked:
            if not locked:
                return
            await self._set_up_boards()
            await self._make_cards()

    async def _set_up_boards(self) -> None:
        for connection in await store.connections_to_set_up(self.engine):
            access = open_access(self.box, connection)
            if access is None:
                continue

            async def on_board(board_id: str, connection: store.Connection = connection) -> None:
                await store.set_board(self.engine, connection, board_id)

            try:
                ready = await board.set_up_board(self.api, access, on_board=on_board)
            except TrelloError as exc:
                log_event(logger, "board_failed", level=logging.WARNING, user_id=connection.user_id, error=str(exc))
                continue
            await store.set_recorded_list(self.engine, connection, ready.recorded_list_id)
            log_event(
                logger,
                "board_ready",
                user_id=connection.user_id,
                board_created=ready.created,
                lists_added=ready.lists_added,
            )

    async def _make_cards(self) -> None:
        waiting = await store.tasks_without_cards(self.engine)
        failed_users: set[int] = set()
        accesses: dict[str, Access | None] = {}
        for task in waiting:
            connection = task.connection
            if connection.user_id in failed_users:
                continue  # Trello refused this user's last one: the rest wait for the next round
            if connection.sealed_access not in accesses:
                accesses[connection.sealed_access] = open_access(self.box, connection)
            access = accesses[connection.sealed_access]
            if access is None:
                failed_users.add(connection.user_id)
                continue
            try:
                await self._card_for(task, access)
            except TrelloError as exc:
                failed_users.add(connection.user_id)
                log_event(
                    logger,
                    "card_failed",
                    level=logging.WARNING,
                    user_id=connection.user_id,
                    task_id=task.task_id,
                    error=str(exc),
                )
                if exc.status in GONE:
                    await store.reset_board(self.engine, connection)

    async def _card_for(self, task: store.TaskWithoutCard, access: Access) -> None:
        connection = task.connection
        if connection.board_id is None or connection.list_id is None:  # never: the query asks for both
            return
        if task.requested_at is not None:

            async def taken(card_ids: list[str]) -> set[str]:
                return await store.cards_taken(self.engine, card_ids)

            found = await board.find_card(
                self.api,
                access,
                connection.board_id,
                task.text,
                made_since=task.requested_at - CLOCK_SLACK,
                taken=taken,
            )
            if found is not None:
                if await store.set_card(self.engine, task.task_id, found):
                    log_event(logger, "card_found", user_id=connection.user_id, task_id=task.task_id)
                return
        await store.mark_card_requested(self.engine, task.task_id)
        card_id = await board.make_card(self.api, access, connection.list_id, task.text)
        if await store.set_card(self.engine, task.task_id, card_id):
            log_event(logger, "card_created", user_id=connection.user_id, task_id=task.task_id)
