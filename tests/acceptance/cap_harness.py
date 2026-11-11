"""What the CAP acceptance tests need beyond SK's, DUP's and ACC's harness (agentify/specs/requirements/CAP.yaml,
`seams`).

- `serve` (after `migrate`) open to everyone and without PostHog: CAP's tests run without an allow list, like SK's,
  so TELEGRAM_ALLOWED_USER_IDS and POSTHOG_PROJECT_TOKEN are removed from the environment whatever the caller's shell
  holds (the state runningBot).
- The screen TaskStore: the table `tasks` with at least `user_id`, `text`, `status` (`recorded` for «записана») and
  `created_at` (timestamptz). A missing table or column is turned into an assertion, not an unhandled error.
- `/tasks` posted and its replies collected: every sendMessage from the post until a bounded quiet period after the
  last one, so that a list sent as several messages is read whole.
- The states annaRecordedTwoTasks and annaRecordedManyLongTasks, reached by driving the bot (never by writing to the
  database).

Black-box like harness.py: nothing here imports the app.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from dup_harness import ANNA, GIVE_UP_AFTER, QUIET_PERIOD
from harness import (
    SAVED_REPLY,
    WEBHOOK_PATH,
    ApiCall,
    AppProcess,
    FakeBotApi,
    TelegramUser,
    command_update,
    deliver,
    text_update,
)
from psycopg.rows import dict_row

# --- The bot's texts, byte for byte from CAP#2 ----------------------------------------

TASKS_HEADER = "Твои задачи:"
NO_TASKS_REPLY = "Задач пока нет."
RECORDED_LABEL = "записана"  # the label of the status `recorded`
RECORDED = "recorded"  # the status in the table tasks
TELEGRAM_MESSAGE_LIMIT = 4096  # characters in one Telegram message

POLL_INTERVAL = 0.05
CLOCK_SLACK = timedelta(minutes=2)  # created_at against the test's clock, as SK's received_at

# The state annaRecordedTwoTasks: Анна wrote these, in this order.
ANNAS_TWO_TASKS = ("Сделать поверку счётчиков", "Записаться к стоматологу")

# Environment the CAP tests must not inherit from the caller's shell (ACC's and ERR's seams).
NOT_INHERITED = ("TELEGRAM_ALLOWED_USER_IDS", "POSTHOG_PROJECT_TOKEN", "POSTHOG_HOST")


def task_line(number: int, text: str) -> str:
    """One line of the list: `<n>. <text> — записана`."""
    return f"{number}. {text} — {RECORDED_LABEL}"


def tasks_list(texts: list[str] | tuple[str, ...]) -> str:
    """The whole list, exactly in CAP#2's form, as one text."""
    return "\n".join([TASKS_HEADER, *(task_line(n, text) for n, text in enumerate(texts, 1))])


def long_task_texts(count: int = 40, length: int = 300) -> list[str]:
    """`count` distinct task texts of exactly `length` characters, each naming its number first, so a list shows its
    order; no leading or trailing space, no leading `/` (not a command)."""
    filler = "сделать дело и не забыть отметить его в списке, "
    texts = []
    for number in range(1, count + 1):
        head = f"Задача {number:02d}: "
        body = (head + filler * (length // len(filler) + 1))[: length - 1]
        texts.append(body + ".")
    assert all(len(text) == length for text in texts)
    assert len(set(texts)) == count
    return texts


def telegram_length(text: str) -> int:
    """The length Telegram counts against its 4096 limit: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


# --- The app ---------------------------------------------------------------------------


def serve_open_bot(
    launch_app: Callable[..., AppProcess], monkeypatch: pytest.MonkeyPatch, database_url: str
) -> AppProcess:
    """The state runningBot: `migrate`, then `serve`, with no allow list and no PostHog token whatever the caller's
    shell holds. Started with conftest's `launch_app`, so a failing test's report carries the app's output."""
    for name in NOT_INHERITED:
        monkeypatch.delenv(name, raising=False)
    return launch_app(database_url)


# --- The table tasks (TaskStore) ---------------------------------------------------------


def fetch_tasks(database_url: str) -> list[dict[str, Any]]:
    """Every row of the table tasks (user_id, text, status, created_at), oldest first.

    The table or one of the contract's columns missing is an assertion failure, with what the database said.
    """
    with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10) as conn:
        try:
            return conn.execute("SELECT user_id, text, status, created_at FROM tasks ORDER BY created_at").fetchall()
        except psycopg.errors.UndefinedTable as exc:
            raise AssertionError(
                "no table tasks in the database after `migrate`: CAP#1 records a task there "
                f"(user_id, text, status, created_at); the database said: {exc}"
            ) from None
        except psycopg.errors.UndefinedColumn as exc:
            raise AssertionError(
                "the table tasks lacks a column of the contract (user_id bigint, text text, status text, "
                f"created_at timestamptz); the database said: {exc}"
            ) from None


def wait_for_tasks(database_url: str, count: int, *, timeout: float = GIVE_UP_AFTER) -> list[dict[str, Any]]:
    """The tasks once there are at least `count` of them, or whatever is there after `timeout` s."""
    deadline = time.monotonic() + timeout
    while True:
        rows = fetch_tasks(database_url)
        if len(rows) >= count or time.monotonic() >= deadline:
            return rows
        time.sleep(0.1)


def describe_tasks(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "  (the table tasks is empty)"
    return "\n".join(
        f"  user_id={row['user_id']} status={row['status']!r} text={row['text']!r} created_at={row['created_at']!r}"
        for row in rows
    )


def assert_created_now(row: dict[str, Any], sent_at: datetime) -> None:
    """created_at is the time the message came, with a time zone: the server's time or the message's date."""
    created_at = row["created_at"]
    assert isinstance(created_at, datetime), f"tasks.created_at is {created_at!r}, not a timestamp"
    assert created_at.tzinfo is not None, "tasks.created_at must be a timestamptz (with a time zone)"
    now = datetime.now().astimezone()
    assert sent_at - CLOCK_SLACK <= created_at <= now + CLOCK_SLACK, (
        f"tasks.created_at {created_at.isoformat()} is not around the time the message was sent ({sent_at.isoformat()})"
    )


def assert_no_task(database_url: str, what: str) -> None:
    """After `what` (and a quiet period, see settle): the table tasks exists and holds no task at all."""
    rows = fetch_tasks(database_url)
    assert rows == [], f"{what} must not become a task, but the table tasks holds:\n{describe_tasks(rows)}"


def settle(after: float) -> None:
    """Wait until QUIET_PERIOD s after the moment `after` (time.monotonic()).

    A bounded quiet period: that no task (or no further reply) comes cannot be polled for.
    """
    remaining = after + QUIET_PERIOD - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


# --- /tasks and its replies ----------------------------------------------------------------


@dataclass
class Answered:
    """Every sendMessage the bot made after one update was posted, in the order they reached the fake Bot API."""

    calls: list[ApiCall]

    @property
    def replies(self) -> list[tuple[str, Any]]:
        """(chat_id as text, text) of every sendMessage."""
        return [(str(call.params.get("chat_id")), call.params.get("text")) for call in self.calls]

    @property
    def texts(self) -> list[Any]:
        return [call.params.get("text") for call in self.calls]


def post_and_collect(
    app: AppProcess,
    api: FakeBotApi,
    update: dict[str, Any],
    *,
    done: Callable[[list[ApiCall]], bool] = bool,
) -> Answered:
    """POST `update` (the contract's 200 expected) and collect every sendMessage after it.

    Polls until `done(calls)` (by default: one sendMessage came), giving up GIVE_UP_AFTER s after the post; then
    waits until no further sendMessage has come for QUIET_PERIOD s, so that a later message is counted too.
    """
    since = api.mark()
    posted_at = time.monotonic()
    response = app.post_update(update)
    assert response.status_code == 200, (
        f"POST {WEBHOOK_PATH} answered {response.status_code}, the contract says 200; body: {response.text!r}\n"
        f"{app.output()}"
    )
    deadline = posted_at + GIVE_UP_AFTER
    while not done(api.send_messages(since=since)) and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
    quiet_until_cap = time.monotonic() + GIVE_UP_AFTER  # a bot that keeps sending cannot hold the test forever
    while time.monotonic() < quiet_until_cap:
        calls = api.send_messages(since=since)
        last = calls[-1].received_at if calls else posted_at
        if time.monotonic() >= last + QUIET_PERIOD:
            break
        time.sleep(POLL_INTERVAL)
    return Answered(api.send_messages(since=since))


def ask_for_tasks(
    app: AppProcess, api: FakeBotApi, user: TelegramUser, *, done: Callable[[list[ApiCall]], bool] = bool
) -> Answered:
    """`user` sends /tasks (as Telegram sends a command: with its bot_command entity)."""
    return post_and_collect(app, api, command_update(user, "/tasks"), done=done)


def list_lines(texts: list[Any]) -> list[str]:
    """The lines of a list sent as several messages, in order: each message's lines, without the line breaks
    Telegram trims at a message's ends."""
    return [line for text in texts for line in str(text).strip("\n").split("\n")]


def first_difference(lines: list[str], expected: list[str]) -> str:
    """Where `lines` first differ from `expected`, for a failure message."""
    at = next(
        (n for n, (got, want) in enumerate(zip(lines, expected, strict=False)) if got != want),
        min(len(lines), len(expected)),
    )
    got = repr(lines[at][:80]) if at < len(lines) else "(nothing)"
    want = repr(expected[at][:80]) if at < len(expected) else "(nothing)"
    return f"they hold {len(lines)} lines, {len(expected)} expected; line {at + 1} is {got}, expected {want}"


# --- States --------------------------------------------------------------------------------


def record_tasks(app: AppProcess, api: FakeBotApi, user: TelegramUser, texts: list[str] | tuple[str, ...]) -> None:
    """`user` writes each text as a plain text message, one by one, each answered «Сохранил ✅» before the next."""
    for number, text in enumerate(texts, 1):
        delivery = deliver(app, api, text_update(user, text))
        assert delivery.reply_text == SAVED_REPLY, (
            f"while {user.first_name} recorded tasks, message {number} of {len(texts)} ({text[:40]!r}…) "
            f"was answered {delivery.reply_text!r}, expected «{SAVED_REPLY}»"
        )


def anna_recorded_two_tasks(app: AppProcess, api: FakeBotApi) -> None:
    """State annaRecordedTwoTasks: Анна (5812345678) wrote «Сделать поверку счётчиков», then «Записаться к
    стоматологу»."""
    record_tasks(app, api, ANNA, ANNAS_TWO_TASKS)
