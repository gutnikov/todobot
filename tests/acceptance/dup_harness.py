"""What the DUP acceptance tests need beyond SK's harness (agentify/specs/requirements/DUP.yaml, `seams`).

- Telegram's repeat of an update: the very same Update JSON posted to the webhook again (DUP#1).
- A fake Bot API that holds its answer to `sendMessage`, so the order of the webhook's 200 and the reply shows (DUP#2).
- Inbox items counted by `user_id` and `text`.

Black-box like harness.py: nothing here imports the app.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg
from harness import WEBHOOK_PATH, AppProcess, FakeBotApi, TelegramUser, fetch_inbox, find_records
from psycopg.rows import dict_row

# The users of the examples' states (runningBot, borisSavedAMessage).
ANNA = TelegramUser(id=5_812_345_678, first_name="Анна", username="anna_tasks")
BORIS = TelegramUser(id=402_117_993, first_name="Борис")

GIVE_UP_AFTER = 10.0  # seconds; like the SK tests' wait for a reply
QUIET_PERIOD = 2.0  # seconds after the webhook answered a repeat in which no reply may come
SEND_MESSAGE_HOLD = 1.5  # seconds the holding fake keeps a sendMessage call before it answers


class HoldingBotApi(FakeBotApi):
    """SK's fake Bot API, except that it answers `sendMessage` only `hold` seconds after the call reached it.

    The call is logged with the moment it reached the fake (`ApiCall.received_at`, `time.monotonic()`), so a test
    can compare it with the moment the webhook answered. Other methods are answered at once.
    """

    def __init__(self, hold: float = SEND_MESSAGE_HOLD) -> None:
        super().__init__()
        self.hold = hold

    def _message_result(self, params: dict[str, Any]) -> dict[str, Any]:
        time.sleep(self.hold)  # the app's sendMessage call stays open meanwhile
        return super()._message_result(params)


@dataclass
class WebhookAnswer:
    response: httpx.Response
    posted_at: float  # time.monotonic() just before the POST
    answered_at: float  # time.monotonic() once the webhook's answer had arrived
    inbox: list[dict[str, Any]] | None  # the inbox read the moment the webhook answered, when asked for


def post_update_timed(app: AppProcess, update: dict[str, Any], *, read_inbox_of: str | None = None) -> WebhookAnswer:
    """POST `update` to the webhook and note when it answered.

    With `read_inbox_of` (a database URL), the inbox is read the moment the answer arrives, on a connection opened
    before the POST, so the snapshot shows what was stored by then.
    """
    if read_inbox_of is None:
        posted_at = time.monotonic()
        response = app.post_update(update)
        return WebhookAnswer(response, posted_at, time.monotonic(), None)
    with psycopg.connect(read_inbox_of, autocommit=True, row_factory=dict_row, connect_timeout=10) as conn:
        posted_at = time.monotonic()
        response = app.post_update(update)
        answered_at = time.monotonic()
        inbox = conn.execute("SELECT user_id, kind, text, file_id, received_at FROM inbox").fetchall()
    return WebhookAnswer(response, posted_at, answered_at, inbox)


def post_again(app: AppProcess, update: dict[str, Any]) -> float:
    """Telegram's repeat: POST the very same Update (same update_id, chat and message_id) again and expect 200.

    Returns the moment (time.monotonic()) the webhook answered.
    """
    answer = post_update_timed(app, update)
    assert answer.response.status_code == 200, (
        f"POST {WEBHOOK_PATH} of the repeated update answered {answer.response.status_code}, the contract says 200; "
        f"body: {answer.response.text!r}"
    )
    return answer.answered_at


def count_items(rows: list[dict[str, Any]], user_id: int, text: str) -> int:
    """How many inbox items `user_id` has with exactly this `text`."""
    return sum(1 for row in rows if row["user_id"] == user_id and row["text"] == text)


def describe_inbox(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "  (the inbox is empty)"
    return "\n".join(f"  user_id={row['user_id']} kind={row['kind']!r} text={row['text']!r}" for row in rows)


def wait_until_repeat_handled(
    app: AppProcess,
    api: FakeBotApi,
    database_url: str,
    *,
    user: TelegramUser,
    text: str,
    since: int,
    answered_at: float,
) -> None:
    """Wait for the repeated update to be handled, then for a quiet period, before the test counts.

    Handled: its `inbox_repeat` line is on stdout, or (what the test then reports as a failure) a reply to the
    user's chat or a second inbox item with `text` shows up; giving up GIVE_UP_AFTER s after the webhook answered.
    Then, so that a late reply is not missed, until QUIET_PERIOD s after the webhook answered.
    """
    deadline = answered_at + GIVE_UP_AFTER
    while time.monotonic() < deadline:
        if find_records(app.stdout_lines, event="inbox_repeat"):
            break
        if api.send_messages(user.id, since=since):
            break
        if count_items(fetch_inbox(database_url), user.id, text) > 1:
            break
        time.sleep(0.1)
    remaining = answered_at + QUIET_PERIOD - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
