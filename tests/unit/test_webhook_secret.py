"""The webhook handles an update only with the right X-Telegram-Bot-Api-Secret-Token.

In-process: the real aiohttp app on a real (migrated) Postgres, with a recording fake Bot API. Each case sends the
rejected update first and then an accepted one from someone else as a control: once the control's reply has gone
out, the rejected update would have been stored and answered by then, had it been handled at all.
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest
from aiogram import Bot, Dispatcher
from fakes import HEADER, SECRET, TOKEN, message_update, running_app

from todobot import texts
from todobot.web import WEBHOOK_PATH, WebhookHandler

INTRUDER = 111_111_111  # sends with a wrong or missing secret
OWNER = 222_222_222  # sends with the right one: the control


async def _post_rejected_then_control(database_url: str, rejected_headers: dict[str, str]) -> tuple[int, int, list]:
    async with running_app(database_url) as (client, api):
        rejected = await client.post(
            WEBHOOK_PATH, json=message_update(INTRUDER, text="не моё"), headers=rejected_headers
        )
        accepted = await client.post(WEBHOOK_PATH, json=message_update(OWNER, text="моё"), headers={HEADER: SECRET})
        await api.first_call()
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
