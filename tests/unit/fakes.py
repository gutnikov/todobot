"""Shared by the unit tests: Telegram updates, a recording fake Bot API, and the app running in-process."""

from __future__ import annotations

import asyncio
import gc
import itertools
import logging
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from todobot.config import Settings
from todobot.logs import JsonFormatter
from todobot.web import build_app

TOKEN = "123456789:unit-test-token"
SECRET = "unit_webhook_secret_9f2c"
HEADER = "X-Telegram-Bot-Api-Secret-Token"
VERSION = "unit"

_ids = itertools.count(1)


def message_update(user_id: int, **content: Any) -> dict[str, Any]:
    """An Update with a private-chat message from `user_id`; `content` is the message's payload."""
    return {
        "update_id": next(_ids),
        "message": {
            "message_id": next(_ids),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "chat": {"id": user_id, "type": "private", "first_name": "Test"},
            "date": int(time.time()),
            **content,
        },
    }


class FakeBotApi:
    """Records `/bot<token>/<method>` calls; a Message for sendMessage, `true` for anything else.

    `hold=True`: a sendMessage call is recorded when it arrives and answered only once `release` is set.
    `refuse=True`: every call is answered with Telegram's 400, as when it refuses a message.
    """

    def __init__(self, *, hold: bool = False, refuse: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.called = asyncio.Event()
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.refuse = refuse
        self.app = web.Application()
        self.app.router.add_post("/bot{token}/{method}", self.handle)

    async def handle(self, request: web.Request) -> web.Response:
        form = {key: str(value) for key, value in (await request.post()).items()}
        method = request.match_info["method"]
        self.calls.append((method, form))
        self.called.set()
        if self.refuse:
            refusal = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
            return web.json_response(refusal, status=400)
        result: Any = True
        if method == "sendMessage":
            await self.release.wait()
            chat = {"id": int(form["chat_id"]), "type": "private"}
            result = {"message_id": len(self.calls), "date": int(time.time()), "chat": chat, "text": form["text"]}
        return web.json_response({"ok": True, "result": result})

    async def first_call(self) -> None:
        async with asyncio.timeout(10):
            await self.called.wait()


def app_settings(database_url: str, api_server: TestServer) -> Settings:
    """What `serve` would get, with the Bot API at `api_server`."""
    return Settings(
        database_url=database_url,
        bot_token=TOKEN,
        webhook_secret=SECRET,
        api_base=str(api_server.make_url("")),
        version=VERSION,
    )


@asynccontextmanager
async def running_app(
    database_url: str, api: FakeBotApi | None = None
) -> AsyncIterator[tuple[TestClient[Any, Any], FakeBotApi]]:
    """The real app (`build_app`) on `database_url`, its Bot API calls going to `api` (a fresh FakeBotApi)."""
    api = api or FakeBotApi()
    async with (
        TestServer(api.app) as api_server,
        TestClient(TestServer(build_app(app_settings(database_url, api_server)))) as client,
    ):
        yield client, api


@contextmanager
def logs_on_stdout() -> Iterator[None]:
    """Log as `serve` does (the production formatter, on stdout, from INFO), for capsys to read."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(VERSION))
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield
        gc.collect()  # asyncio logs a failed task nobody awaited when it is collected: make that happen in here
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
