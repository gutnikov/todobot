"""Tasks in Trello (TRL, agentify/specs/requirements/TRL.yaml), as the app wires them: only with TRELLO_API_KEY set.

- `api`: Trello's HTTP API, OAuth 1.0a signed.
- `connect`: `/trello` and the OAuth callback (TRL#1).
- `board`: the board and its lists (TRL#2), a task's card (TRL#3), the board read for /tasks (TRL#4).
- `sync`: the loop in `serve` that sets boards up and makes the cards, outside the webhook (TRL#2, TRL#3).
- `store`: all of it in the database.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncEngine

from todobot import tasks, texts
from todobot.config import TrelloSettings
from todobot.crypto import SecretBox
from todobot.logs import log_event
from todobot.trello import board, store
from todobot.trello.api import TrelloApi, TrelloError
from todobot.trello.connect import Connector
from todobot.trello.sync import CardSync, open_access

logger = logging.getLogger(__name__)

# Trello's answer to a board that is not there (any more).
NOT_FOUND = 404


class TrelloService:
    """TRL for the handlers and the web app: the link for `/trello`, the callback, the list for `/tasks`, and the
    loop that makes the cards (`nudge` it when there is a new task)."""

    def __init__(self, settings: TrelloSettings, engine: AsyncEngine, bot: Bot) -> None:
        self.settings = settings
        self.engine = engine
        self.api = TrelloApi(settings)
        self.box = SecretBox(settings.secret_key)
        self.sync = CardSync(engine, self.api, self.box, retry_seconds=settings.retry_seconds)
        self.connector = Connector(engine, self.api, self.box, bot, on_connected=self.sync.nudge)

    # the app's life

    async def start(self, app: web.Application | None = None) -> None:
        """On the web app's startup: the HTTP session to Trello, and the loop."""
        await self.api.start()
        self.sync.start()

    async def stop_sync(self, app: web.Application | None = None) -> None:
        """On shutdown: the loop stops at once. A card it was making is found on the board next time."""
        await self.sync.stop()

    async def close(self, app: web.Application | None = None) -> None:
        """On cleanup, once the requests in flight are done: the HTTP session to Trello."""
        await self.sync.stop()
        await self.api.close()

    # TRL#1

    async def link(self, user_id: int) -> str:
        return await self.connector.link(user_id)

    async def callback(self, request: web.Request) -> web.Response:
        return await self.connector.callback(request)

    # TRL#3

    def nudge(self) -> None:
        """A task was recorded: make its card now rather than at the next tick."""
        self.sync.nudge()

    # TRL#4

    async def listed(self, user_id: int) -> list[tuple[str, str]] | None:
        """The user's tasks as /tasks shows them, oldest first: (text, label) each; None when Trello does not answer.

        Connected, a task with a card is its card's current name and the name of the card's list in lower case; a card
        archived or deleted is not shown. A task with no card yet, and every task of a user who has not connected
        Trello, is its text and «ещё не в Trello».
        """
        user_tasks = await tasks.of_user(self.engine, user_id)
        connection = await store.connection_of(self.engine, user_id)
        access = None if connection is None else open_access(self.box, connection)
        if connection is None or access is None or connection.board_id is None:
            return [(task.text, texts.NOT_IN_TRELLO) for task in user_tasks]
        try:
            cards = await board.read_cards(self.api, access, connection.board_id)
        except TrelloError as exc:
            if exc.status != NOT_FOUND:
                log_event(logger, "trello_unavailable", level=logging.WARNING, user_id=user_id, error=str(exc))
                return None
            # The board is gone: so are its cards. It is set up again (a new one) for the tasks to come.
            log_event(logger, "board_gone", level=logging.WARNING, user_id=user_id)
            await store.reset_board(self.engine, connection)
            self.sync.nudge()
            cards = {}
        listed: list[tuple[str, str]] = []
        for task in user_tasks:
            if task.card_id is None:
                listed.append((task.text, texts.NOT_IN_TRELLO))
            elif (card := cards.get(task.card_id)) is not None:
                listed.append((card.name, card.list_name.lower()))
        return listed
