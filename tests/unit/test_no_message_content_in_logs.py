"""What users write never reaches the logs, also when storing it fails (SK-5's handler_error line).

Each case breaks the schema so that the inbox INSERT itself fails with the message among its parameters (an
unmigrated database would fail earlier, on `users`, before the text is ever sent). Then the app's stdout, written
by the production formatter, must not hold the text or the caption in any form: whole, or cut short.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import psycopg
import pytest
from fakes import HEADER, SECRET, VERSION, logs_on_stdout, message_update, running_app

from todobot import texts
from todobot.logs import REDACTED, redact
from todobot.web import WEBHOOK_PATH

USER = 333_333_333

# Each line starts with a marker: renderings that clip a value keep its start. The first line is longer than the
# 64 bytes Postgres keeps of a value in "Failing row contains (...)".
TEXT = "QX7-тайна: первая строка, достаточно длинная, чтобы Postgres её обрезал\nQY8-тайна: вторая строка"
CAPTION = "QZ9-подпись к фото, тоже секрет"
MARKERS = ("QX7", "QY8", "QZ9")

PHOTO = [{"file_id": "photo-x", "file_unique_id": "u-x", "width": 1280, "height": 960}]

BREAKAGES = {
    # The INSERT fails with the text among its bound parameters.
    "inbox-missing": ("DROP TABLE inbox", 'relation "inbox" does not exist'),
    # Postgres rejects the row and quotes it back: DETAIL: Failing row contains (..., <text>, ...).
    "row-rejected": ("ALTER TABLE inbox ADD COLUMN must_have integer NOT NULL", "violates not-null constraint"),
}


async def _deliver(database_url: str, update: dict) -> list[tuple[str, dict[str, str]]]:
    async with running_app(database_url) as (client, api):
        response = await client.post(WEBHOOK_PATH, json=update, headers={HEADER: SECRET})
        assert response.status == 200
        await api.first_call()
    return api.calls


@pytest.mark.parametrize("breakage", sorted(BREAKAGES))
@pytest.mark.parametrize(
    "content",
    [pytest.param({"text": TEXT}, id="text"), pytest.param({"photo": PHOTO, "caption": CAPTION}, id="caption")],
)
def test_failed_insert_logs_handler_error_without_the_message(migrated_database_url, capsys, breakage, content):
    statement, diagnosis = BREAKAGES[breakage]
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        conn.execute(statement)

    with logs_on_stdout():
        calls = asyncio.run(_deliver(migrated_database_url, message_update(USER, **content)))
    stdout = capsys.readouterr().out

    assert calls == [("sendMessage", {"chat_id": str(USER), "text": texts.ERROR})]
    records = [json.loads(line) for line in stdout.splitlines()]
    errors = [record for record in records if record["event"] == "handler_error"]
    assert len(errors) == 1, stdout
    assert errors[0]["level"] == "ERROR"
    assert errors[0]["version"] == VERSION
    assert "Traceback" in errors[0]["exc"]
    assert diagnosis in errors[0]["exc"], "the redacted traceback should still say what went wrong"
    assert "[parameters:" not in errors[0]["exc"], "database errors must not carry the statement's parameters"
    for marker in MARKERS:
        assert marker not in stdout


# An update the Bot API types cannot validate: pydantic's error quotes its input, the user's text among it.
# Markers in this file are ASCII: the JSON lines escape non-ASCII text, so a Cyrillic marker would hide from `in`.
INVALID_UPDATES = {
    "message-fields-missing": ({"update_id": 77, "message": {"text": "QV5-тайна в сообщении"}}, 77),
    "message-not-an-object": ({"update_id": 78, "message": "QV5-тайна вместо сообщения"}, 78),
    "update-not-an-object": (["QV5-тайна вместо апдейта"], None),
}


async def _post_only(database_url: str, body: Any) -> tuple[int, list[tuple[str, dict[str, str]]]]:
    async with running_app(database_url) as (client, api):
        response = await client.post(WEBHOOK_PATH, json=body, headers={HEADER: SECRET})
    # Leaving running_app shut the app down, which waits for the updates it was still handling.
    return response.status, api.calls


@pytest.mark.parametrize("case", sorted(INVALID_UPDATES))
def test_invalid_update_logs_one_line_without_its_content(migrated_database_url, capsys, case):
    body, update_id = INVALID_UPDATES[case]

    with logs_on_stdout():
        status, calls = asyncio.run(_post_only(migrated_database_url, body))
    stdout = capsys.readouterr().out

    assert status == 200  # the webhook's answer is as before
    assert calls == []  # nothing was handled
    records = [json.loads(line) for line in stdout.splitlines()]
    invalid = [record for record in records if record["event"] == "update_invalid"]
    assert len(invalid) == 1, stdout
    assert invalid[0]["level"] == "ERROR"
    assert invalid[0]["version"] == VERSION
    assert invalid[0]["error"] == "ValidationError"
    assert invalid[0]["update_id"] == update_id
    assert "QV5" not in stdout
    assert "never retrieved" not in stdout


@pytest.mark.parametrize(
    ("logged", "expected"),
    [
        pytest.param(
            "DETAIL:  Failing row contains (1, text, Купить молоко и хлеб до пятницы обязательно, null).",
            f"DETAIL:  Failing row contains (1, text, {REDACTED}",
            id="whole",
        ),
        pytest.param(
            "DETAIL:  Failing row contains (1, text, Купить молоко и хлеб до пя, null).",
            f"DETAIL:  Failing row contains (1, text, {REDACTED}",
            id="clipped-by-postgres",
        ),
        pytest.param(
            "invalid input for query argument $3: 'Купить молоко и хлеб до пятницы\\nобя... (bad)",
            f"invalid input for query argument $3: '{REDACTED}",
            id="repr-clipped-by-asyncpg",
        ),
        pytest.param("сначала строка\nобязательно, null)", f"сначала строка\n{REDACTED}", id="second-line"),
        pytest.param("relation inbox does not exist", "relation inbox does not exist", id="unrelated-kept"),
    ],
)
def test_redact(logged: str, expected: str) -> None:
    secret = "Купить молоко и хлеб до пятницы\nобязательно"
    assert redact(logged, [secret, None]) == expected


def test_redact_without_secrets_changes_nothing() -> None:
    assert redact("a\nb", [None, ""]) == "a\nb"
