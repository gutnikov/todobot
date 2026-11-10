"""SK-1 /start: the greeting, within 5 seconds, and its `start` log line."""

from __future__ import annotations

import pytest

from harness import (
    APP_VERSION,
    START_REPLY,
    TelegramUser,
    command_update,
    deliver,
    find_records,
)

pytestmark = pytest.mark.acceptance

ANNA = TelegramUser(id=5_812_345_678, first_name="Анна", username="anna_tasks")


def test_sk1_start_replies_with_greeting_within_5_seconds(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    delivery = deliver(app, fake_api, command_update(ANNA, "/start"), within=5.0)

    assert delivery.reply_text == START_REPLY
    assert delivery.elapsed <= 5.0, f"the greeting reached the Bot API {delivery.elapsed:.2f} s after the update"


def test_sk1_start_writes_start_log_line_with_version(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    deliver(app, fake_api, command_update(ANNA, "/start"), within=5.0)

    lines = app.stop()

    assert find_records(lines, event="start", version=APP_VERSION), (
        f"no stdout line with event=start and version={APP_VERSION!r}; stdout was:\n" + "\n".join(lines)
    )


# scratch: an approved test changed without an H5 approval
