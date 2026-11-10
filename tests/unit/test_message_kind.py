"""SK-2's table: a message's kind, text and file_id."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.types import Message

from todobot.inbox import InboxItem, classify

USER = {"id": 42, "is_bot": False, "first_name": "Анна"}
CHAT = {"id": 42, "type": "private", "first_name": "Анна"}

PHOTO_SIZES = [
    {"file_id": "photo-s", "file_unique_id": "u-s", "width": 90, "height": 68},
    {"file_id": "photo-m", "file_unique_id": "u-m", "width": 320, "height": 240},
    {"file_id": "photo-x", "file_unique_id": "u-x", "width": 1280, "height": 960},
]
VOICE = {"file_id": "voice-1", "file_unique_id": "u-v", "duration": 3}
DOCUMENT = {"file_id": "doc-1", "file_unique_id": "u-d", "file_name": "list.pdf"}
STICKER = {
    "file_id": "sticker-1",
    "file_unique_id": "u-st",
    "type": "regular",
    "width": 512,
    "height": 512,
    "is_animated": False,
    "is_video": False,
}


def message(**content: Any) -> Message:
    return Message.model_validate({"message_id": 1, "date": 1_700_000_000, "chat": CHAT, "from": USER, **content})


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param({"text": "Купить молоко"}, InboxItem("text", "Купить молоко", None), id="text"),
        pytest.param(
            {"text": "/help", "entities": [{"type": "bot_command", "offset": 0, "length": 5}]},
            InboxItem("text", "/help", None),
            id="other-command",
        ),
        pytest.param({"voice": VOICE}, InboxItem("voice", None, "voice-1"), id="voice"),
        pytest.param(
            {"voice": VOICE, "caption": "Позвонить маме"},
            InboxItem("voice", "Позвонить маме", "voice-1"),
            id="voice-with-caption",
        ),
        pytest.param({"photo": PHOTO_SIZES}, InboxItem("photo", None, "photo-x"), id="photo-largest-is-last"),
        pytest.param(
            {"photo": PHOTO_SIZES, "caption": "Чек"}, InboxItem("photo", "Чек", "photo-x"), id="photo-with-caption"
        ),
        pytest.param({"sticker": STICKER}, InboxItem("other", None, None), id="sticker"),
        pytest.param({"document": DOCUMENT}, InboxItem("other", None, None), id="document"),
        pytest.param(
            {"document": DOCUMENT, "caption": "Список"}, InboxItem("other", "Список", None), id="document-with-caption"
        ),
        pytest.param(
            {"location": {"latitude": 55.75, "longitude": 37.62}}, InboxItem("other", None, None), id="location"
        ),
    ],
)
def test_classify(content: dict[str, Any], expected: InboxItem) -> None:
    assert classify(message(**content)) == expected
