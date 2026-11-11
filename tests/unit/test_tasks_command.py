"""CAP in the running app: a plain text becomes a task (CAP#1) and `/tasks` lists the sender's (CAP#2), with their log
lines; and what users wrote stays out of the logs on both ways, also when they fail."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import psycopg
import pytest
from aiohttp import web
from fakes import HEADER, SECRET, VERSION, FakeBotApi, logs_on_stdout, message_update, running_app

from todobot import inbox, texts
from todobot.db import create_engine
from todobot.inbox import InboxItem
from todobot.web import WEBHOOK_PATH

ANNA = 5_812_345_678
BORIS = 402_117_993
# Markers are ASCII: the JSON lines escape Cyrillic, which would hide it from `in`.
FIRST = "QT1-тайна: сделать поверку счётчиков"
SECOND = "QT2-тайна: записаться к стоматологу"
MARKERS = ("QT1", "QT2")
BOTH_LISTED = f"Твои задачи:\n1. {FIRST} — записана\n2. {SECOND} — записана"


def command(text: str) -> dict[str, Any]:
    """A command's text with Telegram's bot_command entity."""
    return {"text": text, "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}


async def _post_all(
    database_url: str, *updates: dict[str, Any], api: FakeBotApi | None = None
) -> list[tuple[str, dict[str, str]]]:
    async with running_app(database_url, api) as (client, api):
        for update in updates:
            response = await client.post(WEBHOOK_PATH, json=update, headers={HEADER: SECRET})
            assert response.status == 200
    # The webhook answers once the update is handled (DUP#2): by the answer, anything it would call has been called.
    return api.calls


def _post(database_url: str, *updates: dict[str, Any], api: FakeBotApi | None = None) -> list[tuple[str, Any]]:
    return asyncio.run(_post_all(database_url, *updates, api=api))


def _rows(database_url: str) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """The inbox (user_id, kind, text) and the tasks (user_id, text, status)."""
    with psycopg.connect(database_url) as conn:
        items = conn.execute("SELECT user_id, kind, text FROM inbox ORDER BY received_at").fetchall()
        tasks = conn.execute("SELECT user_id, text, status FROM tasks ORDER BY created_at, id").fetchall()
    return items, tasks


def _records(stdout: str, event: str) -> list[dict[str, Any]]:
    return [record for line in stdout.splitlines() if (record := json.loads(line))["event"] == event]


def sent(chat: int, text: str) -> tuple[str, dict[str, str]]:
    return ("sendMessage", {"chat_id": str(chat), "text": text})


# --- CAP#1 -----------------------------------------------------------------------------------------------------------


def test_a_plain_text_is_saved_recorded_and_logged_without_its_text(migrated_database_url, capsys) -> None:
    update = message_update(ANNA, text=FIRST)

    with logs_on_stdout():
        calls = _post(migrated_database_url, update)
    stdout = capsys.readouterr().out

    assert calls == [sent(ANNA, texts.SAVED)]
    assert _rows(migrated_database_url) == ([(ANNA, "text", FIRST)], [(ANNA, FIRST, "recorded")])
    recorded = _records(stdout, "task_recorded")
    assert len(recorded) == 1, stdout
    assert {key: recorded[0][key] for key in ("level", "version", "user_id", "message_id")} == {
        "level": "INFO",
        "version": VERSION,
        "user_id": ANNA,
        "message_id": update["message"]["message_id"],
    }
    for marker in MARKERS:
        assert marker not in stdout


def test_a_repeat_records_and_logs_no_second_task(migrated_database_url, capsys) -> None:
    update = message_update(ANNA, text=FIRST)

    with logs_on_stdout():
        calls = _post(migrated_database_url, update, update)
    stdout = capsys.readouterr().out

    assert calls == [sent(ANNA, texts.SAVED)]
    assert _rows(migrated_database_url)[1] == [(ANNA, FIRST, "recorded")]
    assert len(_records(stdout, "task_recorded")) == 1
    assert len(_records(stdout, "inbox_repeat")) == 1


def test_a_command_is_an_inbox_item_only_and_logs_no_task(migrated_database_url, capsys) -> None:
    with logs_on_stdout():
        calls = _post(migrated_database_url, message_update(ANNA, **command("/help")))
    stdout = capsys.readouterr().out

    assert calls == [sent(ANNA, texts.SAVED)]
    assert _rows(migrated_database_url) == ([(ANNA, "text", "/help")], [])
    assert not _records(stdout, "task_recorded")


def test_a_task_that_cannot_be_stored_is_apologised_for_without_its_text_in_the_logs(
    migrated_database_url, capsys
) -> None:
    """The task's INSERT fails and Postgres quotes the rejected row, the text among it: handler_error cuts it out, and
    the inbox item is not stored either (one transaction)."""
    with psycopg.connect(migrated_database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE tasks ADD COLUMN must_have integer NOT NULL")

    with logs_on_stdout():
        calls = _post(migrated_database_url, message_update(ANNA, text=FIRST))
    stdout = capsys.readouterr().out

    assert calls == [sent(ANNA, texts.ERROR)]
    assert _rows(migrated_database_url) == ([], [])
    errors = _records(stdout, "handler_error")
    assert len(errors) == 1, stdout
    assert "violates not-null constraint" in errors[0]["exc"]
    assert not _records(stdout, "task_recorded")
    for marker in MARKERS:
        assert marker not in stdout


# --- CAP#2 -----------------------------------------------------------------------------------------------------------


def test_tasks_lists_the_senders_tasks_and_stores_nothing(migrated_database_url, capsys) -> None:
    with logs_on_stdout():
        calls = _post(
            migrated_database_url,
            message_update(ANNA, text=FIRST),
            message_update(BORIS, text="Забрать посылку"),
            message_update(ANNA, text=SECOND),
            message_update(ANNA, **command("/tasks")),
        )
    stdout = capsys.readouterr().out

    assert calls[-1] == sent(ANNA, BOTH_LISTED)
    items, tasks = _rows(migrated_database_url)
    assert len(items) == 3  # /tasks is no inbox item ...
    assert len(tasks) == 3  # ... and no task
    listed = _records(stdout, "tasks_listed")
    assert len(listed) == 1, stdout
    assert {key: listed[0][key] for key in ("level", "version", "user_id", "tasks", "messages")} == {
        "level": "INFO",
        "version": VERSION,
        "user_id": ANNA,
        "tasks": 2,
        "messages": 1,
    }
    assert len(_records(stdout, "inbox_saved")) == 3  # the three messages, not /tasks
    for marker in MARKERS:
        assert marker not in stdout


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(command("/tasks"), id="tasks"),
        pytest.param(command("/tasks@todobot_bot"), id="with-the-bots-name"),
        pytest.param(command("/tasks все"), id="with-words-after-it"),
    ],
)
def test_tasks_with_none_recorded(migrated_database_url, content) -> None:
    calls = _post(migrated_database_url, message_update(ANNA, **content))

    assert calls == [sent(ANNA, texts.NO_TASKS)]
    assert _rows(migrated_database_url) == ([], [])


def test_only_the_senders_tasks(migrated_database_url) -> None:
    calls = _post(migrated_database_url, message_update(ANNA, text=FIRST), message_update(BORIS, **command("/tasks")))

    assert calls == [sent(ANNA, texts.SAVED), sent(BORIS, texts.NO_TASKS)]


def test_a_photo_captioned_tasks_is_stored_like_any_other(migrated_database_url) -> None:
    photo = [{"file_id": "photo-x", "file_unique_id": "u-x", "width": 1280, "height": 960}]
    caption = command("/tasks")
    content = {"photo": photo, "caption": caption["text"], "caption_entities": caption["entities"]}

    calls = _post(migrated_database_url, message_update(ANNA, **content))

    assert calls == [sent(ANNA, texts.SAVED)]
    assert _rows(migrated_database_url) == ([(ANNA, "photo", "/tasks")], [])


async def _record(database_url: str, user_id: int, task_texts: list[str]) -> None:
    engine = create_engine(database_url)
    try:
        for message_id, text in enumerate(task_texts, 1):
            await inbox.save(engine, user_id, message_id, InboxItem("text", text, None))
    finally:
        await engine.dispose()


def test_a_long_list_is_sent_as_several_messages_in_order(migrated_database_url, capsys) -> None:
    task_texts = [f"Задача {number}: " + "дело " * 300 for number in range(1, 4)]  # ~1510 characters each
    asyncio.run(_record(migrated_database_url, ANNA, task_texts))

    with logs_on_stdout():
        calls = _post(migrated_database_url, message_update(ANNA, **command("/tasks")))
    stdout = capsys.readouterr().out

    lines = [f"{number}. {text} — записана" for number, text in enumerate(task_texts, 1)]
    assert calls == [sent(ANNA, "\n".join(["Твои задачи:", *lines[:2]])), sent(ANNA, lines[2])]
    assert _records(stdout, "tasks_listed")[0]["messages"] == 2


class MalformedAnswers(FakeBotApi):
    """Answers sendMessage with a Message that does not validate (no chat), echoing the text, as a Bot API that changed
    under aiogram would: aiogram's ClientDecodeError then quotes the whole answer."""

    async def handle(self, request: web.Request) -> web.Response:
        form = {key: str(value) for key, value in (await request.post()).items()}
        self.calls.append((request.match_info["method"], form))
        self.called.set()
        result = {"message_id": len(self.calls), "date": int(time.time()), "text": form.get("text")}
        return web.json_response({"ok": True, "result": result})


def test_a_list_that_cannot_be_sent_is_logged_without_the_tasks(migrated_database_url, capsys) -> None:
    asyncio.run(_record(migrated_database_url, ANNA, [FIRST, SECOND]))

    with logs_on_stdout():
        calls = _post(migrated_database_url, message_update(ANNA, **command("/tasks")), api=MalformedAnswers())
    stdout = capsys.readouterr().out

    for marker in MARKERS:
        assert marker not in stdout
    assert calls == [sent(ANNA, BOTH_LISTED), sent(ANNA, texts.ERROR)]
    errors = _records(stdout, "handler_error")
    assert len(errors) == 1, stdout
    assert errors[0]["error"] == "TasksNotSentError"
    assert "message 1 of 1: ClientDecodeError" in errors[0]["exc"]
    assert not _records(stdout, "tasks_listed")
