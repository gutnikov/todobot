"""ACC: the allow list (TELEGRAM_ALLOWED_USER_IDS), parsed; a stranger refused, in-process on a real Postgres; the line
`serve` writes at start; and `serve` not starting on a value that is not a list of ids."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from typing import Any

import psycopg
import pytest
from fakes import HEADER, SECRET, VERSION, FakeBotApi, logs_on_stdout, message_update, running_app

from todobot import texts
from todobot.config import ALLOWED_USER_IDS, ConfigError, Settings, allowed_user_ids
from todobot.web import WEBHOOK_PATH, log_access

OWNER = 5_812_345_678
STRANGER = 402_117_993
OWNER_ONLY = frozenset({OWNER})
TEXT = "QA3-тайна: забрать посылку"  # the marker is ASCII: the JSON lines escape Cyrillic
CAPTION = "QC5-подпись к фото"
PHOTO = [{"file_id": "QF7-photo-file-id", "file_unique_id": "u-x", "width": 1280, "height": 960}]
MARKERS = ("QA3", "QC5", "QF7")

REQUIRED = {
    "DATABASE_URL": "postgresql://u:p@db.local/app",
    "TELEGRAM_BOT_TOKEN": "123:token",
    "TELEGRAM_WEBHOOK_SECRET": "s3cret",
}


# --- Parsing ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("5812345678", frozenset({OWNER}), id="one"),
        pytest.param("5812345678,402117993", frozenset({OWNER, STRANGER}), id="two"),
        pytest.param(" 5812345678 ,  402117993 ", frozenset({OWNER, STRANGER}), id="spaces"),
        pytest.param("5812345678, 5812345678", frozenset({OWNER}), id="repeated"),
    ],
)
def test_allow_list_is_the_ids(raw: str, expected: frozenset[int]) -> None:
    assert allowed_user_ids({ALLOWED_USER_IDS: raw}) == expected


@pytest.mark.parametrize("env", [{}, {ALLOWED_USER_IDS: ""}, {ALLOWED_USER_IDS: "  "}], ids=["unset", "empty", "blank"])
def test_without_an_allow_list_every_user_is_served(env: dict[str, str]) -> None:
    assert allowed_user_ids(env) is None
    assert Settings.from_env({**REQUIRED, **env}).allowed_user_ids is None


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("anna", id="a-name"),
        pytest.param("5812345678;402117993", id="semicolons"),
        pytest.param("5812345678 402117993", id="spaces-only"),
        pytest.param("5812345678,", id="trailing-comma"),
        pytest.param("5812345678,,402117993", id="empty-item"),
        pytest.param("-5812345678", id="negative"),
        pytest.param("0", id="zero"),
        pytest.param("5812345678.0", id="decimal"),
        pytest.param("0x15a", id="hex"),
        pytest.param("5_812_345_678", id="underscores"),
        pytest.param("٥٨١٢", id="non-ascii-digits"),
    ],
)
def test_anything_else_is_a_config_error_that_does_not_quote_the_value(raw: str) -> None:
    with pytest.raises(ConfigError, match=ALLOWED_USER_IDS) as raised:
        Settings.from_env({**REQUIRED, ALLOWED_USER_IDS: raw})
    # config_error goes to PostHog, which gets no user id: the message says where, not what
    assert raw.strip(",") not in str(raised.value)
    assert "5812345678" not in str(raised.value)


def test_settings_read_the_allow_list_and_keep_it_out_of_repr() -> None:
    settings = Settings.from_env({**REQUIRED, ALLOWED_USER_IDS: "5812345678, 402117993"})

    assert settings.allowed_user_ids == frozenset({OWNER, STRANGER})
    assert "5812345678" not in repr(settings)


# --- The refusal -----------------------------------------------------------------------------------------------------


async def _post(
    database_url: str, update: dict[str, Any], api: FakeBotApi | None = None
) -> list[tuple[str, dict[str, str]]]:
    async with running_app(database_url, api, allowed_user_ids=OWNER_ONLY) as (client, api):
        response = await client.post(WEBHOOK_PATH, json=update, headers={HEADER: SECRET})
        assert response.status == 200
    # The webhook answers once the update is handled (DUP#2): by the answer, anything it would call has been called.
    return api.calls


def _stored(database_url: str) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """The users and the inbox rows."""
    with psycopg.connect(database_url) as conn:
        users = conn.execute("SELECT id FROM users").fetchall()
        inbox = conn.execute("SELECT user_id, kind, text FROM inbox").fetchall()
    return users, inbox


def _records(stdout: str, event: str) -> list[dict[str, Any]]:
    return [record for line in stdout.splitlines() if (record := json.loads(line))["event"] == event]


@pytest.mark.parametrize(
    "content",
    [
        pytest.param({"text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}, id="start"),
        pytest.param({"text": TEXT}, id="text"),
        pytest.param({"photo": PHOTO, "caption": CAPTION}, id="photo"),
        pytest.param({"location": {"latitude": 52.37, "longitude": 4.89}}, id="other"),
    ],
)
def test_a_stranger_gets_the_refusal_only_and_nothing_is_stored(
    migrated_database_url: str, capsys: pytest.CaptureFixture[str], content: dict[str, Any]
) -> None:
    update = message_update(STRANGER, **content)

    with logs_on_stdout():
        calls = asyncio.run(_post(migrated_database_url, update))
    stdout = capsys.readouterr().out

    assert calls == [("sendMessage", {"chat_id": str(STRANGER), "text": texts.REFUSED})]
    assert _stored(migrated_database_url) == ([], [])
    denied = _records(stdout, "access_denied")
    assert len(denied) == 1, stdout
    assert {key: denied[0][key] for key in ("level", "version", "user_id", "message_id")} == {
        "level": "INFO",
        "version": VERSION,
        "user_id": STRANGER,
        "message_id": update["message"]["message_id"],
    }
    assert not _records(stdout, "start")
    assert not _records(stdout, "inbox_saved")
    for marker in MARKERS:  # what the stranger wrote never reaches the logs
        assert marker not in stdout


def test_a_listed_user_is_served_as_before(migrated_database_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    with logs_on_stdout():
        calls = asyncio.run(_post(migrated_database_url, message_update(OWNER, text=TEXT)))
    stdout = capsys.readouterr().out

    assert calls == [("sendMessage", {"chat_id": str(OWNER), "text": texts.SAVED})]
    assert _stored(migrated_database_url) == ([(OWNER,)], [(OWNER, "text", TEXT)])
    assert not _records(stdout, "access_denied")


def test_a_stranger_in_a_group_is_left_unhandled_as_before(
    migrated_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bot is for private chats: the allow list does not make it answer anywhere else."""
    update = {
        "update_id": 9_001,
        "message": {
            "message_id": 9_002,
            "from": {"id": STRANGER, "is_bot": False, "first_name": "Test"},
            "chat": {"id": -100_123, "type": "group", "title": "Group"},
            "date": int(time.time()),
            "text": TEXT,
        },
    }

    with logs_on_stdout():
        calls = asyncio.run(_post(migrated_database_url, update))
    stdout = capsys.readouterr().out

    assert calls == []
    assert _stored(migrated_database_url) == ([], [])
    assert not _records(stdout, "access_denied")


def test_a_refusal_telegram_does_not_take_is_logged_and_not_apologised_for(
    migrated_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with logs_on_stdout():
        calls = asyncio.run(_post(migrated_database_url, message_update(STRANGER, text=TEXT), FakeBotApi(refuse=True)))
    stdout = capsys.readouterr().out

    assert calls == [("sendMessage", {"chat_id": str(STRANGER), "text": texts.REFUSED})]  # no apology after it
    assert _stored(migrated_database_url) == ([], [])
    failed = _records(stdout, "refusal_failed")
    assert len(failed) == 1, stdout
    assert failed[0]["level"] == "ERROR"
    assert failed[0]["error"] == "TelegramBadRequest"
    assert not _records(stdout, "handler_error")
    assert "QA3" not in stdout


# --- At start --------------------------------------------------------------------------------------------------------


def test_restricted_start_says_how_many_users(capsys: pytest.CaptureFixture[str]) -> None:
    with logs_on_stdout():
        log_access(frozenset({OWNER, STRANGER}))
    stdout = capsys.readouterr().out

    restricted = _records(stdout, "access_restricted")
    assert [(record["level"], record["allowed"]) for record in restricted] == [("INFO", 2)]
    assert str(OWNER) not in stdout


def test_open_start_is_a_warning_not_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    """WARNING: it goes to stdout only; an ERROR line would also go to PostHog (ERR#1)."""
    with logs_on_stdout():
        log_access(None)
    stdout = capsys.readouterr().out

    assert [record["level"] for record in _records(stdout, "access_open")] == ["WARNING"]
    assert not _records(stdout, "access_restricted")


def test_serve_does_not_start_on_a_value_that_is_not_a_list_of_ids() -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("POSTHOG_", "TELEGRAM_"))}
    env.update(REQUIRED, PORT="0", **{ALLOWED_USER_IDS: "5812345678; 402117993"})

    result = subprocess.run(
        [sys.executable, "-m", "todobot", "serve"], env=env, capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 2, result.stdout + result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(record["level"], record["event"]) for record in records] == [("ERROR", "config_error")], result.stdout
    assert ALLOWED_USER_IDS in records[0]["error"]
    assert "5812345678" not in result.stdout
    assert "402117993" not in result.stdout
