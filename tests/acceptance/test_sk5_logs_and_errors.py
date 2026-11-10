"""SK-5 Logs and errors: JSON lines on stdout, handler_error and the apology, and the process survives.

The trigger is the contract's: `serve` on a fresh database where `migrate` has not run, so storing a message fails.
"""

from __future__ import annotations

import pytest

from harness import (
    APP_VERSION,
    ERROR_REPLY,
    TelegramUser,
    command_update,
    deliver,
    find_records,
    log_line_violations,
    text_update,
)

pytestmark = pytest.mark.acceptance

ANNA = TelegramUser(id=5_812_345_678, first_name="Анна", username="anna_tasks")
BORIS = TelegramUser(id=402_117_993, first_name="Борис")


def test_sk5_every_stdout_line_is_json_with_level_event_version(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    deliver(app, fake_api, command_update(ANNA, "/start"), within=5.0)
    deliver(app, fake_api, text_update(ANNA, "Купить молоко и хлеб"))
    assert app.get("/healthz").status_code == 200

    lines = app.stop()

    assert lines, "serve wrote nothing to stdout (SK-1 and SK-2 each require a log line)"
    problems = log_line_violations(lines)
    assert not problems, "\n".join(problems)


def test_sk5_handler_error_writes_error_line_and_sends_apology(launch_app, fake_api, database_url):
    app = launch_app(database_url, migrate=False)  # SK-5's trigger
    delivery = deliver(app, fake_api, text_update(ANNA, "Купить молоко и хлеб"))

    assert delivery.reply_text == ERROR_REPLY
    lines = app.stop()
    assert find_records(lines, level="ERROR", event="handler_error", version=APP_VERSION), (
        f"no stdout line with level=ERROR, event=handler_error and version={APP_VERSION!r}; stdout was:\n"
        + "\n".join(lines)
    )


def test_sk5_stdout_stays_json_lines_when_a_handler_fails(launch_app, fake_api, database_url):
    app = launch_app(database_url, migrate=False)  # SK-5's trigger
    deliver(app, fake_api, text_update(ANNA, "Купить молоко и хлеб"))

    lines = app.stop()

    assert lines, "serve wrote nothing to stdout"
    problems = log_line_violations(lines)
    assert not problems, "\n".join(problems)


def test_sk5_process_keeps_handling_updates_and_healthz_after_handler_error(launch_app, fake_api, database_url):
    app = launch_app(database_url, migrate=False)  # SK-5's trigger
    first = deliver(app, fake_api, text_update(ANNA, "Купить молоко и хлеб"))
    assert first.reply_text == ERROR_REPLY
    assert app.is_running(), f"serve exited with code {app.returncode} after a handler error"

    # The next update is handled: it gets its own reply (the database is still unmigrated, so the same apology).
    second = deliver(app, fake_api, text_update(BORIS, "Забрать посылку"))
    assert second.reply_text == ERROR_REPLY

    health = app.get("/healthz")
    assert health.status_code == 200, f"body: {health.text!r}"
    assert health.json() == {"status": "ok", "version": APP_VERSION}
    assert app.is_running()
