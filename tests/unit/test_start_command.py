"""/start is a text message (SK-1); a photo or a voice note captioned "/start" is stored like any other (SK-2)."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest
from fakes import HEADER, SECRET, message_update, running_app

from todobot import texts
from todobot.web import WEBHOOK_PATH

USER = 444_444_444

PHOTO = [
    {"file_id": "photo-s", "file_unique_id": "u-s", "width": 90, "height": 68},
    {"file_id": "photo-x", "file_unique_id": "u-x", "width": 1280, "height": 960},
]
VOICE = {"file_id": "voice-1", "file_unique_id": "u-v", "duration": 3}


def command_entities(command: str) -> list[dict[str, Any]]:
    """How Telegram marks a command at the start of a text or a caption."""
    return [{"type": "bot_command", "offset": 0, "length": len(command.split()[0])}]


async def _send(database_url: str, update: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    async with running_app(database_url) as (client, api):
        response = await client.post(WEBHOOK_PATH, json=update, headers={HEADER: SECRET})
        assert response.status == 200
        await api.first_call()
    return api.calls


def _inbox(database_url: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(database_url) as conn:
        return conn.execute("SELECT user_id, kind, text, file_id FROM inbox").fetchall()


@pytest.mark.parametrize(
    ("content", "row"),
    [
        pytest.param(
            {"photo": PHOTO, "caption": "/start", "caption_entities": command_entities("/start")},
            (USER, "photo", "/start", "photo-x"),
            id="photo",
        ),
        pytest.param(
            {"voice": VOICE, "caption": "/start напомни", "caption_entities": command_entities("/start напомни")},
            (USER, "voice", "/start напомни", "voice-1"),
            id="voice",
        ),
    ],
)
def test_media_captioned_start_is_stored(migrated_database_url: str, content: dict[str, Any], row: tuple) -> None:
    calls = asyncio.run(_send(migrated_database_url, message_update(USER, **content)))

    assert calls == [("sendMessage", {"chat_id": str(USER), "text": texts.SAVED})]
    assert _inbox(migrated_database_url) == [row]


def test_text_start_gets_the_greeting_and_stores_nothing(migrated_database_url: str) -> None:
    update = message_update(USER, text="/start", entities=command_entities("/start"))

    calls = asyncio.run(_send(migrated_database_url, update))

    assert calls == [("sendMessage", {"chat_id": str(USER), "text": texts.START})]
    assert _inbox(migrated_database_url) == []
