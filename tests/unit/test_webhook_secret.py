"""The webhook handles an update only with the right X-Telegram-Bot-Api-Secret-Token.

In-process: the real aiohttp app on a real (migrated) Postgres, with a recording fake Bot API. Each case sends the
rejected update first and then an accepted one from someone else as a control: once the control's reply has gone
out, the rejected update would have been stored and answered by then, had it been handled at all.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any

import psycopg
import pytest
from aiogram import Bot, Dispatcher
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from todobot import texts
from todobot.config import Settings
from todobot.web import WEBHOOK_PATH, WebhookHandler, build_app

TOKEN = "123456789:unit-test-token"
SECRET = "unit_webhook_secret_9f2c"
HEADER = "X-Telegram-Bot-Api-Secret-Token"

INTRUDER = 111_111_111  # sends with a wrong or missing secret
OWNER = 222_222_222  # sends with the right one: the control

_update_ids = itertools.count(1)


def text_update(user_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_update_ids),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "chat": {"id": user_id, "type": "private", "first_name": "Test"},
            "date": int(time.time()),
            "text": text,
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


async def _post_rejected_then_control(database_url: str, rejected_headers: dict[str, str]) -> tuple[int, int, list]:
    api = FakeBotApi()
    async with TestServer(api.app) as api_server:
        settings = Settings(
            database_url=database_url,
            bot_token=TOKEN,
            webhook_secret=SECRET,
            api_base=str(api_server.make_url("")),
            version="unit",
        )
        async with TestClient(TestServer(build_app(settings))) as client:
            rejected = await client.post(WEBHOOK_PATH, json=text_update(INTRUDER, "не моё"), headers=rejected_headers)
            accepted = await client.post(WEBHOOK_PATH, json=text_update(OWNER, "моё"), headers={HEADER: SECRET})
            await asyncio.wait_for(api.called.wait(), timeout=10)
    return rejected.status, accepted.status, api.calls


def _inbox(database_url: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(database_url) as conn:
        return conn.execute("SELECT user_id, kind, text FROM inbox ORDER BY received_at").fetchall()


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing"),
        pytest.param({HEADER: ""}, id="empty"),
        pytest.param({HEADER: "wrong_secret"}, id="wrong"),
        pytest.param({HEADER: SECRET[:-1]}, id="prefix"),
        pytest.param({HEADER: SECRET + "x"}, id="longer"),
        pytest.param({HEADER: SECRET.upper()}, id="other-case"),
    ],
)
def test_update_without_the_right_secret_is_rejected_and_not_handled(migrated_database_url, headers):
    rejected, accepted, calls = asyncio.run(_post_rejected_then_control(migrated_database_url, headers))

    assert rejected == 401
    assert accepted == 200
    # Only the control was answered and stored; the rejected update left no trace.
    assert calls == [("sendMessage", {"chat_id": str(OWNER), "text": texts.SAVED})]
    assert _inbox(migrated_database_url) == [(OWNER, "text", "моё")]


def test_handler_refuses_an_empty_secret():
    with pytest.raises(ValueError, match="secret"):
        WebhookHandler(Dispatcher(), Bot(TOKEN), secret_token="")
