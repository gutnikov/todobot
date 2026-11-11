"""CAP#2 `/tasks` replies with the sender's tasks, oldest first, exactly as «Твои задачи:\\n1. <text> — записана…»,
or «Задач пока нет.» when the sender has none; a list longer than one Telegram message is sent as several messages, each
starting where the last ended; only the sender's own tasks are shown; `/tasks` is neither an inbox item nor a task.
Slice S05's examples in agentify/specs/requirements/CAP.yaml.

States, reached by driving the bot: runningBot (nobody has written yet; the user is Анна, 5812345678),
annaRecordedTwoTasks (Анна wrote «Сделать поверку счётчиков», then «Записаться к стоматологу»),
borisAsksAfterAnnasTasks (as annaRecordedTwoTasks, and /tasks comes from Борис, 402117993) and
annaRecordedManyLongTasks (Анна wrote 40 distinct texts of 300 characters each).
"""

from __future__ import annotations

import pytest
from acc_harness import wait_for_records
from cap_harness import (
    NO_TASKS_REPLY,
    TASKS_HEADER,
    TELEGRAM_MESSAGE_LIMIT,
    anna_recorded_two_tasks,
    ask_for_tasks,
    assert_no_task,
    first_difference,
    list_lines,
    long_task_texts,
    record_tasks,
    serve_open_bot,
    task_line,
    telegram_length,
)
from dup_harness import ANNA, BORIS, describe_inbox
from harness import APP_VERSION, fetch_inbox

pytestmark = pytest.mark.acceptance

# The reply of the example, byte for byte.
ANNAS_TWO_TASKS_LIST = "Твои задачи:\n1. Сделать поверку счётчиков — записана\n2. Записаться к стоматологу — записана"


def test_cap2_tasks_list_shows_recorded_tasks_oldest_first(launch_app, monkeypatch, fake_api, database_url):
    """S05 «tasks list shows recorded tasks oldest first», state annaRecordedTwoTasks (CAP#2)."""
    app = serve_open_bot(launch_app, monkeypatch, database_url)
    anna_recorded_two_tasks(app, fake_api)

    answered = ask_for_tasks(app, fake_api, ANNA)

    expected = [(str(ANNA.id), ANNAS_TWO_TASKS_LIST)]
    assert answered.replies == expected, (
        f"Анна's /tasks was answered {answered.replies!r} as (chat_id, text); expected exactly {expected!r}: her two "
        f"tasks, oldest first, in one message to her chat\nBot API calls:\n{fake_api.describe_calls()}"
    )

    listed = wait_for_records(app, event="tasks_listed")
    assert listed, "no stdout line with event=tasks_listed for /tasks; stdout was:\n" + "\n".join(app.stdout_lines)
    assert all(record.get("version") == APP_VERSION for record in listed), (
        f"the event=tasks_listed lines must carry version={APP_VERSION!r}: {listed!r}"
    )


def test_cap2_empty_tasks_list(launch_app, monkeypatch, fake_api, database_url):
    """S05 «empty tasks list», state runningBot (CAP#2)."""
    app = serve_open_bot(launch_app, monkeypatch, database_url)

    answered = ask_for_tasks(app, fake_api, ANNA)

    expected = [(str(ANNA.id), NO_TASKS_REPLY)]
    assert answered.replies == expected, (
        f"/tasks with no tasks recorded was answered {answered.replies!r} as (chat_id, text); expected exactly "
        f"{expected!r}\nBot API calls:\n{fake_api.describe_calls()}"
    )


def test_cap2_tasks_list_shows_only_the_sender_s_tasks(launch_app, monkeypatch, fake_api, database_url):
    """S05 «tasks list shows only the sender's tasks», state borisAsksAfterAnnasTasks (CAP#2)."""
    app = serve_open_bot(launch_app, monkeypatch, database_url)
    anna_recorded_two_tasks(app, fake_api)

    answered = ask_for_tasks(app, fake_api, BORIS)

    expected = [(str(BORIS.id), NO_TASKS_REPLY)]
    assert answered.replies == expected, (
        f"Борис's /tasks (he recorded nothing; Анна recorded two tasks) was answered {answered.replies!r} as "
        f"(chat_id, text); expected exactly {expected!r}, in his chat only\n"
        f"Bot API calls:\n{fake_api.describe_calls()}"
    )


def test_cap2_tasks_is_neither_an_inbox_item_nor_a_task(launch_app, monkeypatch, fake_api, database_url):
    """S05 «/tasks is neither an inbox item nor a task», state runningBot (CAP#2)."""
    app = serve_open_bot(launch_app, monkeypatch, database_url)

    ask_for_tasks(app, fake_api, ANNA)  # waits for the reply, then a quiet period

    inbox = fetch_inbox(database_url)
    assert inbox == [], "/tasks must not be an inbox item, but the inbox holds:\n" + describe_inbox(inbox)
    assert_no_task(database_url, "/tasks")


def test_cap2_long_tasks_list_is_split_into_several_messages(launch_app, monkeypatch, fake_api, database_url):
    """S05 «long tasks list is split into several messages», state annaRecordedManyLongTasks (CAP#2)."""
    app = serve_open_bot(launch_app, monkeypatch, database_url)
    texts = long_task_texts(count=40, length=300)
    record_tasks(app, fake_api, ANNA, texts)
    expected_lines = [TASKS_HEADER, *(task_line(number, text) for number, text in enumerate(texts, 1))]

    def whole_list_came(calls) -> bool:
        return set(expected_lines) <= set(list_lines([call.params.get("text") for call in calls]))

    answered = ask_for_tasks(app, fake_api, ANNA, done=whole_list_came)
    messages = answered.texts

    assert messages, f"Анна's /tasks got no reply\nBot API calls:\n{fake_api.describe_calls()}"
    assert {chat for chat, _ in answered.replies} == {str(ANNA.id)}, (
        f"the list must go to Анна's chat {ANNA.id} only; replies went to {sorted({c for c, _ in answered.replies})}"
    )
    assert str(messages[0]).startswith(TASKS_HEADER), (
        f"the first message of Анна's list must start with «{TASKS_HEADER}»; it is {str(messages[0])[:120]!r} "
        f"({len(messages)} message(s) in all)"
    )
    too_long = [
        (number, telegram_length(str(m)))
        for number, m in enumerate(messages, 1)
        if telegram_length(str(m)) > TELEGRAM_MESSAGE_LIMIT
    ]
    assert not too_long, (
        f"each message must be at most {TELEGRAM_MESSAGE_LIMIT} characters; (message, length) over it: {too_long!r}"
    )
    assert len(messages) >= 2, (
        f"a list of 40 tasks of 300 characters is longer than one Telegram message, so it must come as several "
        f"messages; it came as {len(messages)}"
    )

    lines = list_lines(messages)
    assert lines == expected_lines, (
        f"together the {len(messages)} messages must hold «{TASKS_HEADER}» and all 40 lines in order, each exactly "
        f"once, and nothing else; {first_difference(lines, expected_lines)}"
    )
