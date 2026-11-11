"""ERR in-process, against a local fake PostHog (fakes.FakePostHog): no network.

- ERR#1: a line with level ERROR or CRITICAL goes to PostHog as one `$exception` made from the log line: its event,
  error class and traceback as stdout has them, and the version; distinct_id `todobot`, no person profile. INFO and
  WARNING lines do not go, nor a line's other fields. Through the real app, a rejected row's DETAIL reaches PostHog
  without what the user wrote and without their id.
- ERR#2: `server_started` with the version.
- ERR#3: a refusing PostHog is a WARNING line `posthog_failed` and no ERROR line; the SDK's own lines stay at WARNING
  and never go back to PostHog; stopping waits for a hanging PostHog at most CLOSE_SECONDS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from pathlib import Path
from typing import Any

import aiogram
import psycopg
import pytest
from fakes import HEADER, SECRET, VERSION, FakePostHog, logs_on_stdout, message_update, running_app

import todobot
from todobot import error_tracking, texts
from todobot.config import DEFAULT_POSTHOG_HOST, PostHogSettings
from todobot.error_tracking import errors_to_posthog, exception_properties, parse_traceback
from todobot.logs import REDACTED, format_exception, log_event, redact
from todobot.web import WEBHOOK_PATH

TOKEN = "phc_unit_fake_token"  # fake: PostHog's project token is configuration, never in the repository
USER = 444_444_444
ERROR_LEVELS = {"ERROR", "CRITICAL"}
logger = logging.getLogger("todobot.test")


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """No retries and a short batching wait: a refusing PostHog costs these tests no backoff."""
    monkeypatch.setattr(error_tracking, "MAX_RETRIES", 0)
    monkeypatch.setattr(error_tracking, "FLUSH_INTERVAL_SECONDS", 0.1)


def _settings(posthog: FakePostHog) -> PostHogSettings:
    return PostHogSettings(token=TOKEN, host=posthog.url)


def _records(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines()]


def _raw(posthog: FakePostHog) -> str:
    """Everything PostHog got, as text (the SDK escapes non-ASCII letters: markers here are ASCII)."""
    return "\n".join(body.decode() for body in posthog.bodies)


class StorageError(Exception):
    pass


def _store(text: str) -> None:
    try:
        raise ValueError(f"Failing row contains ({USER}, text, {text}, null).")
    except ValueError as exc:
        raise StorageError("could not store the message") from exc


# --- Configuration -------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [{}, {"POSTHOG_PROJECT_TOKEN": ""}, {"POSTHOG_PROJECT_TOKEN": "  "}, {"POSTHOG_HOST": "http://127.0.0.1:9"}],
)
def test_without_a_token_serve_has_no_posthog(env: dict[str, str]) -> None:
    assert PostHogSettings.from_env(env) is None


def test_posthog_settings_default_to_the_eu_cloud_and_hide_the_token() -> None:
    settings = PostHogSettings.from_env({"POSTHOG_PROJECT_TOKEN": TOKEN})
    assert settings == PostHogSettings(token=TOKEN, host="https://eu.i.posthog.com")
    assert settings.host == DEFAULT_POSTHOG_HOST
    assert TOKEN not in repr(settings)
    custom = PostHogSettings.from_env({"POSTHOG_PROJECT_TOKEN": TOKEN, "POSTHOG_HOST": "http://127.0.0.1:9"})
    assert custom is not None
    assert custom.host == "http://127.0.0.1:9"


# --- ERR#1, ERR#2: what PostHog gets -------------------------------------------------


def test_an_error_line_goes_to_posthog_as_one_exception_made_of_the_line(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "QP1-what the user wrote"
    with FakePostHog() as posthog, logs_on_stdout(), errors_to_posthog(_settings(posthog), VERSION) as tracking:
        tracking.server_started()
        log_event(logger, "inbox_saved", kind="text", user_id=USER, message_id=7)
        log_event(logger, "healthz_failed", level=logging.WARNING, error="TimeoutError: ")
        try:
            _store(secret)
        except StorageError as exc:
            log_event(
                logger,
                "handler_error",
                level=logging.ERROR,
                update_id=99,
                error=type(exc).__name__,
                exc=redact(format_exception(exc), [secret, str(USER)]),
            )
    (line,) = [record for record in _records(capsys.readouterr().out) if record["level"] in ERROR_LEVELS]

    events = posthog.events
    assert [event["event"] for event in events] == ["server_started", "$exception"]
    for event in events:
        assert event["distinct_id"] == "todobot"
        assert event["properties"]["$process_person_profile"] is False
        assert event["properties"]["version"] == VERSION
        assert event["properties"]["$geoip_disable"] is True

    properties = events[1]["properties"]
    assert properties["log_event"] == "handler_error"
    assert properties["log_level"] == "ERROR"
    assert properties["$exception_level"] == "error"
    assert properties["error_class"] == "StorageError"
    assert properties["traceback"] == line["exc"]  # as the log line has it
    outer, cause = properties["$exception_list"]  # outermost first
    assert (outer["type"], outer["module"], outer["value"]) == (
        "StorageError",
        StorageError.__module__,
        "could not store the message",
    )
    assert outer["mechanism"] == {"type": "generic", "handled": True, "synthetic": False}
    assert outer["stacktrace"]["frames"][-1]["function"] == "_store"
    assert (
        outer["stacktrace"]["frames"][-1]["context_line"]
        == 'raise StorageError("could not store the message") from exc'
    )
    assert (cause["type"], cause["module"]) == ("ValueError", None)
    assert cause["value"] == f"Failing row contains ({REDACTED}"
    assert cause["mechanism"]["type"] == "chained"

    raw = _raw(posthog)
    for absent in ("QP1", str(USER), "update_id", "message_id", "inbox_saved", "healthz_failed"):
        assert absent not in raw


def test_a_crash_hooks_line_is_a_fatal_unhandled_exception(capsys: pytest.CaptureFixture[str]) -> None:
    with FakePostHog() as posthog, logs_on_stdout(), errors_to_posthog(_settings(posthog), VERSION):
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:  # as logs.setup_logging's excepthook logs it
            log_event(logging.getLogger("todobot.crash"), "uncaught_exception", level=logging.CRITICAL, exc_info=exc)
    (line,) = _records(capsys.readouterr().out)

    (event,) = posthog.events
    properties = event["properties"]
    assert event["event"] == "$exception"
    assert properties["log_event"] == "uncaught_exception"
    assert properties["$exception_level"] == "fatal"
    assert properties["error_class"] == "RuntimeError"
    assert properties["traceback"] == line["exc"]
    (exception,) = properties["$exception_list"]
    assert (exception["type"], exception["value"]) == ("RuntimeError", "boom")
    assert exception["mechanism"]["handled"] is False


@pytest.mark.parametrize(
    ("line", "kind", "value", "error_class"),
    [
        pytest.param(
            {"event": "update_invalid", "level": "ERROR", "logger": "todobot.bot", "update_id": 7,
             "error": "ValidationError", "problems": ["message.chat: missing"]},
            "ValidationError", "update_invalid", "ValidationError", id="our-line-names-the-class",
        ),
        pytest.param(
            {"event": "config_error", "level": "ERROR", "logger": "todobot", "error": "TELEGRAM_BOT_TOKEN is required"},
            "config_error", "TELEGRAM_BOT_TOKEN is required", None, id="our-line-says-what",
        ),
        pytest.param(
            {"event": "log", "level": "ERROR", "logger": "aiohttp.server", "message": "Error handling request from x"},
            "aiohttp.server", "log", None, id="a-librarys-line",
        ),
    ],
)  # fmt: skip
def test_a_line_without_a_traceback_is_what_the_line_says(
    line: dict[str, Any], kind: str, value: str, error_class: str | None
) -> None:
    properties = exception_properties(line)
    (exception,) = properties["$exception_list"]
    assert (exception["type"], exception["value"]) == (kind, value)
    assert properties.get("error_class") == error_class
    assert properties["log_event"] == line["event"]
    assert "traceback" not in properties
    sent = json.dumps(properties)
    for absent in ("update_id", "problems", "message.chat", "Error handling request"):
        assert absent not in sent


def test_parse_traceback_names_the_frames_and_the_apps_own() -> None:
    router = Path(aiogram.__file__).parent / "dispatcher" / "router.py"
    bot = Path(todobot.__file__).parent / "bot.py"
    text = "\n".join(
        [
            "Traceback (most recent call last):",
            f'  File "{router}", line 192, in _propagate_event',
            "    response = await observer.trigger(event, **kwargs)",
            "               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^",
            f'  File "{bot}", line 120, in on_message',
            "    if not await inbox.save(engine, user_id, message.message_id, item):",
            "  [Previous line repeated 2 more times]",
            "sqlalchemy.exc.ProgrammingError: (sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError) <class "
            "'asyncpg.exceptions.UndefinedTableError'>: relation \"users\" does not exist",
            "[SQL: INSERT INTO users (id) VALUES ($1::BIGINT)]",
        ]
    )
    (exception,) = parse_traceback(text)
    assert (exception["type"], exception["module"]) == ("ProgrammingError", "sqlalchemy.exc")
    assert exception["value"].endswith(
        'relation "users" does not exist\n[SQL: INSERT INTO users (id) VALUES ($1::BIGINT)]'
    )
    library, ours = exception["stacktrace"]["frames"]
    assert (library["filename"], library["module"], library["in_app"]) == (
        "aiogram/dispatcher/router.py",
        "aiogram.dispatcher.router",
        False,
    )
    assert library["context_line"] == "response = await observer.trigger(event, **kwargs)"
    assert (ours["filename"], ours["module"], ours["in_app"], ours["lineno"]) == (
        "todobot/bot.py",
        "todobot.bot",
        True,
        120,
    )


def test_what_python_did_not_write_as_a_plain_traceback_falls_back_to_the_line() -> None:
    group = ExceptionGroup("several", [ValueError("a"), KeyError("b")])
    for text in ("not a traceback at all", "".join(traceback.format_exception(group))):
        assert parse_traceback(text) == []
    properties = exception_properties(
        {"event": "handler_error", "level": "ERROR", "logger": "todobot.bot", "error": "ExceptionGroup", "exc": "x y"}
    )
    assert properties["$exception_list"][0]["type"] == "ExceptionGroup"
    assert properties["traceback"] == "x y"


def test_a_rejected_row_reaches_posthog_without_what_the_user_wrote_or_their_id(
    migrated_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Postgres rejects the row and quotes it back (DETAIL: Failing row contains (<user id>, text, <text>, ...))."""
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE inbox ADD COLUMN must_have integer NOT NULL")
    text = "QX7-what the user wrote, long enough for Postgres to clip it in the DETAIL of the error"

    async def deliver() -> list[tuple[str, dict[str, str]]]:
        async with running_app(migrated_database_url) as (client, api):
            response = await client.post(WEBHOOK_PATH, json=message_update(USER, text=text), headers={HEADER: SECRET})
            assert response.status == 200  # answered once handled (DUP#2): the apology is through
        return api.calls

    with FakePostHog() as posthog, logs_on_stdout(), errors_to_posthog(_settings(posthog), VERSION):
        calls = asyncio.run(deliver())
    stdout = capsys.readouterr().out

    assert calls == [("sendMessage", {"chat_id": str(USER), "text": texts.ERROR})]
    errors = [record for record in _records(stdout) if record["level"] in ERROR_LEVELS]
    exceptions = [event for event in posthog.events if event["event"] == "$exception"]
    assert len(exceptions) == len(errors) == 1, stdout
    properties = exceptions[0]["properties"]
    assert properties["log_event"] == errors[0]["event"] == "handler_error"
    assert properties["traceback"] == errors[0]["exc"]
    assert "violates not-null constraint" in properties["traceback"], "PostHog should still learn what went wrong"
    assert "Failing row contains" in properties["traceback"]
    for absent in ("QX7", str(USER)):
        assert absent not in _raw(posthog)
        assert absent not in errors[0]["exc"]


# --- ERR#3: PostHog refusing ---------------------------------------------------------


def test_a_refusing_posthog_is_a_warning_line_posthog_failed_and_no_error_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        FakePostHog(refuse=True) as posthog,
        logs_on_stdout(),
        errors_to_posthog(_settings(posthog), VERSION) as tracking,
    ):
        tracking.server_started()
        log_event(logger, "handler_error", level=logging.ERROR, error="ProgrammingError")
    records = _records(capsys.readouterr().out)

    failed = [record for record in records if record["event"] == "posthog_failed"]
    assert failed
    assert {(record["level"], record["error"], record["status"]) for record in failed} == {("WARNING", "APIError", 500)}
    assert [record["event"] for record in records if record["level"] in ERROR_LEVELS] == ["handler_error"]
    assert all(not key.startswith("_") for record in records for key in record)
    assert set(posthog.statuses) == {500}
    # each event tried once (no retries here), and nothing about the failure sent after it
    assert sorted(event["event"] for event in posthog.events) == ["$exception", "server_started"]


def test_the_sdks_own_lines_stay_at_warning_and_away_from_posthog(capsys: pytest.CaptureFixture[str]) -> None:
    with FakePostHog() as posthog, logs_on_stdout(), errors_to_posthog(_settings(posthog), VERSION):
        logging.getLogger("posthog").error("error uploading: %s", "refused")
        logging.getLogger("posthog").critical("worse")
    sdk = [record for record in _records(capsys.readouterr().out) if record["logger"] == "posthog"]

    assert [record["level"] for record in sdk] == ["WARNING", "WARNING"]
    assert posthog.events == []


def test_stopping_waits_for_a_hanging_posthog_at_most_close_seconds(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(error_tracking, "CLOSE_SECONDS", 0.5)
    with FakePostHog(hang=True) as posthog, logs_on_stdout():
        with errors_to_posthog(_settings(posthog), VERSION) as tracking:
            tracking.server_started()
            stopping = time.monotonic()
        took = time.monotonic() - stopping
    records = _records(capsys.readouterr().out)

    assert 0.5 <= took < 2.0
    assert [(r["level"], r["error"]) for r in records if r["event"] == "posthog_failed"] == [
        ("WARNING", "TimeoutError")
    ]
    assert not [record for record in records if record["level"] in ERROR_LEVELS]
