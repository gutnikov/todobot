"""What the ACC acceptance tests need beyond SK's, DUP's and ERR's harness (agentify/specs/requirements/ACC.yaml,
`seams`).

- `serve` started with TELEGRAM_ALLOWED_USER_IDS naming Anna (5812345678) only: the state closedBot. Or started with
  the variable removed from the environment, whatever the caller's shell holds: the state runningBot, open to all.
- An update handled and counted: the first reply waited for (giving up after GIVE_UP_AFTER s), then a bounded quiet
  period, so that a second reply (a greeting, a "saved" acknowledgement) would be counted too.
- stdout lines waited for by their fields.

Black-box like harness.py: nothing here imports the app.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from dup_harness import ANNA
from harness import WEBHOOK_PATH, ApiCall, AppProcess, FakeBotApi, app_env, find_records, free_port, run_migrate

ALLOW_LIST_VAR = "TELEGRAM_ALLOWED_USER_IDS"
OWNER_ONLY = str(ANNA.id)  # closedBot: the allow list names Anna (5812345678) only
REFUSED_REPLY = "Извини, бот пока закрыт."  # the rule reply/refused, byte for byte

GIVE_UP_AFTER = 10.0  # seconds; like the SK tests' wait for a reply
QUIET_PERIOD = 2.0  # seconds after the first reply in which a further reply would still be counted
POLL_INTERVAL = 0.05


@contextmanager
def serve_app(database_url: str, fake_api: FakeBotApi, *, allowed_user_ids: str | None) -> Iterator[AppProcess]:
    """`serve` after `migrate` (Database answers), with TELEGRAM_ALLOWED_USER_IDS = `allowed_user_ids`, or without the
    variable when it is None. `migrate` runs with SK's environment only, as in every SK and DUP example. The app is
    stopped on the way out."""
    port = free_port()
    env = app_env(database_url=database_url, port=port, api_base=fake_api.base_url)
    env.pop(ALLOW_LIST_VAR, None)  # the caller's shell must not decide whether the bot is open
    run_migrate(env)
    serve_env = dict(env) if allowed_user_ids is None else {**env, ALLOW_LIST_VAR: allowed_user_ids}
    app = AppProcess(serve_env, port)
    try:
        yield app.start()
    finally:
        app.stop()


@dataclass
class Handled:
    """What the bot did about one update: every Bot API call it made after the update was posted."""

    response: httpx.Response
    calls: list[ApiCall]

    @property
    def replies(self) -> list[tuple[str, Any]]:
        """(chat_id as text, text) of every sendMessage, in the order they reached the fake Bot API."""
        return [
            (str(call.params.get("chat_id")), call.params.get("text")) for call in self.calls if call.is_send_message
        ]

    @property
    def other_calls(self) -> list[ApiCall]:
        """The Bot API calls that are not a sendMessage (SK's seam: serve calls the Bot API only to reply)."""
        return [call for call in self.calls if not call.is_send_message]


def post_and_settle(app: AppProcess, api: FakeBotApi, update: dict[str, Any]) -> Handled:
    """POST `update` to the webhook (the contract's 200 expected), wait for the first sendMessage to any chat, giving up
    GIVE_UP_AFTER s after the post, then until QUIET_PERIOD s after that reply reached the fake Bot API; returns every
    Bot API call made since the post."""
    since = api.mark()
    posted_at = time.monotonic()
    response = app.post_update(update)
    assert response.status_code == 200, (
        f"POST {WEBHOOK_PATH} answered {response.status_code}, the contract says 200; body: {response.text!r}\n"
        f"{app.output()}"
    )
    deadline = posted_at + GIVE_UP_AFTER
    while not (first := api.send_messages(since=since)) and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
    if first:
        remaining = first[0].received_at + QUIET_PERIOD - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)  # a bounded quiet period: the absence of a further reply cannot be polled for
    return Handled(response, api.calls[since:])


def wait_for_records(app: AppProcess, *, within: float = GIVE_UP_AFTER, **expected: Any) -> list[dict[str, Any]]:
    """The stdout lines with the `expected` fields, as soon as there is one; whatever there is after `within` s or
    once the process has exited."""
    deadline = time.monotonic() + within
    while True:
        records = find_records(app.stdout_lines, **expected)
        if records or time.monotonic() >= deadline or not app.is_running():
            return records
        time.sleep(0.1)


def describe(app: AppProcess, api: FakeBotApi) -> str:
    return f"Bot API calls recorded:\n{api.describe_calls()}\n{app.output()}"
