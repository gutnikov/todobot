"""TRL#1: a user connects their own Trello through OAuth 1.0a.

`/trello` gets a request token from Trello (with the callback `{PUBLIC_BASE_URL}/trello/callback`), keeps it for the
user who asked for REQUEST_TTL, and answers with the link to Trello's authorize page. Trello sends the user's browser
back to the callback with that request token and, when they allowed access, a verifier. The callback finds the user by
the request token, which Trello issued at random and only that user got, never by anything else in its URL; it serves
once and not after REQUEST_TTL. It exchanges token and verifier for the user's access, keeps it encrypted, shows a page
and tells the chat.

Never logged: the link (it holds the request token), the request token, the verifier, the access token, any secret.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from datetime import timedelta

from aiogram import Bot
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncEngine

from todobot import texts
from todobot.crypto import SecretBox
from todobot.logs import log_event
from todobot.trello import store
from todobot.trello.api import TrelloApi, TrelloError

logger = logging.getLogger(__name__)

# How long the link /trello sent works.
REQUEST_TTL = timedelta(minutes=30)


def page(text: str, *, status: int = 200) -> web.Response:
    """The page the browser shows after Trello: one line of text. Not cached, and its address (which holds the request
    token and the verifier) is not passed on as a referrer."""
    body = (
        '<!doctype html>\n<html lang="ru">\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>todobot</title>\n<p>{html.escape(text)}</p>\n</html>\n"
    )
    return web.Response(
        text=body,
        status=status,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


class Connector:
    def __init__(
        self,
        engine: AsyncEngine,
        api: TrelloApi,
        box: SecretBox,
        bot: Bot,
        *,
        on_connected: Callable[[], None],
    ) -> None:
        self.engine = engine
        self.api = api
        self.box = box
        self.bot = bot
        self.on_connected = on_connected

    async def link(self, user_id: int) -> str:
        """`/trello`: the link to Trello's authorize page, for this user only. The user's earlier links stop working."""
        token, secret = await self.api.request_token()
        await store.save_request(
            self.engine, token=token, sealed_secret=self.box.seal(secret), user_id=user_id, ttl=REQUEST_TTL
        )
        return self.api.authorize_url(token)

    async def callback(self, request: web.Request) -> web.Response:
        """`GET /trello/callback`: where Trello sends the user's browser after they decided."""
        try:
            return await self._decided(request.query.get("oauth_token", ""), request.query.get("oauth_verifier", ""))
        except Exception as exc:  # the type only: the request's URL holds the tokens
            log_event(logger, "trello_callback_failed", level=logging.ERROR, error=type(exc).__name__)
            return page(texts.TRELLO_DECLINED_PAGE, status=500)

    async def _decided(self, token: str, verifier: str) -> web.Response:
        pending = await store.take_request(self.engine, token) if token else None
        if pending is None or pending.expired:
            reason = "unknown" if pending is None else "expired"
            log_event(logger, "trello_callback_unknown", level=logging.WARNING, reason=reason)
            return page(texts.TRELLO_DECLINED_PAGE, status=400)
        user_id = pending.user_id
        secret = self.box.open(pending.sealed_secret)
        if secret is None:  # APP_SECRET_KEY changed since /trello
            log_event(logger, "trello_callback_unknown", level=logging.WARNING, reason="unreadable", user_id=user_id)
            return page(texts.TRELLO_DECLINED_PAGE, status=400)

        if not verifier:  # the user refused access: Trello sends no verifier
            log_event(logger, "trello_declined", user_id=user_id)
            await self._tell(user_id, texts.TRELLO_DECLINED)
            return page(texts.TRELLO_DECLINED_PAGE)

        try:
            access = await self.api.access_token(token, secret, verifier)
        except TrelloError as exc:
            log_event(logger, "trello_connect_failed", level=logging.ERROR, user_id=user_id, error=str(exc))
            await self._tell(user_id, texts.ERROR)
            return page(texts.TRELLO_DECLINED_PAGE, status=502)
        await store.save_connection(self.engine, user_id, self.box.seal(access.dumps()))
        self.on_connected()
        log_event(logger, "trello_connected", user_id=user_id)
        await self._tell(user_id, texts.TRELLO_CONNECTED)
        return page(texts.TRELLO_CONNECTED_PAGE)

    async def _tell(self, user_id: int, text: str) -> None:
        """A message to the user's chat (a private chat's id is the user's); one Telegram does not take is logged."""
        try:
            await self.bot.send_message(chat_id=user_id, text=text)
        except Exception as exc:  # the type only: what aiogram raises can quote the text sent
            log_event(logger, "trello_reply_failed", level=logging.ERROR, user_id=user_id, error=type(exc).__name__)
