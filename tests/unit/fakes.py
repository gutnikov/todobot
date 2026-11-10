"""Shared by the unit tests: Telegram updates, a recording fake Bot API, and the app running in-process."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from todobot.config import Settings
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
    """Records `/bot<token>/<method>` calls; a Message for sendMessage, `true` for anything else."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.called = asyncio.Event()
        self.app = web.Application()
        self.app.router.add_post("/bot{token}/{method}", self.handle)

    async def handle(self, request: web.Request) -> web.Response:
        form = {key: str(value) for key, value in (await request.post()).items()}
        method = request.match_info["method"]
        self.calls.append((method, form))
        self.called.set()
        result: Any = True
        if method == "sendMessage":
            chat = {"id": int(form["chat_id"]), "type": "private"}
            result = {"message_id": len(self.calls), "date": int(time.time()), "chat": chat, "text": form["text"]}
        return web.json_response({"ok": True, "result": result})

    async def first_call(self) -> None:
        async with asyncio.timeout(10):
            await self.called.wait()


@asynccontextmanager
async def running_app(database_url: str) -> AsyncIterator[tuple[TestClient[Any, Any], FakeBotApi]]:
    """The real app (`build_app`) on `database_url`, its Bot API calls going to a fresh FakeBotApi."""
    api = FakeBotApi()
    async with TestServer(api.app) as api_server:
        settings = Settings(
            database_url=database_url,
            bot_token=TOKEN,
            webhook_secret=SECRET,
            api_base=str(api_server.make_url("")),
            version=VERSION,
        )
        async with TestClient(TestServer(build_app(settings))) as client:
            yield client, api
