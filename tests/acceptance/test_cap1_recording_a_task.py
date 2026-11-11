"""CAP#1 A plain text message (not a command) becomes one task with status «записана» (`recorded` in the table tasks),
whose text is the message's text; the bot stores the inbox item and replies «Сохранил ✅» as before. A command, a voice
note, a photo or anything else stays an inbox item only; a message with several things is one task; a repeated update
makes no second task. Slice S02's examples in agentify/specs/requirements/CAP.yaml (the rule table becomes-task).

States: runningBot (nobody has written yet; the user is Анна, 5812345678) and borisUpdateComesAgain (Борис, 402117993,
sent «Забрать посылку», and Telegram sends that very update again: same update_id, chat and message_id).
"""

from __future__ import annotations

import pytest
from acc_harness import wait_for_records
from cap_harness import (
    RECORDED,
    assert_created_now,
    assert_no_task,
    describe_tasks,
    fetch_tasks,
    serve_open_bot,
    settle,
    wait_for_tasks,
)
from dup_harness import ANNA, BORIS, describe_inbox, post_again, wait_until_repeat_handled
from harness import (
    APP_VERSION,
    SAVED_REPLY,
    command_update,
    deliver,
    fetch_inbox,
    photo_sizes,
    photo_update,
    sticker_update,
    text_update,
    voice_update,
    wait_for_inbox_rows,
)

pytestmark = pytest.mark.acceptance


def test_cap1_plain_text_becomes_a_recorded_task(launch_app, monkeypatch, fake_api, database_url):
    """S02 «plain text becomes a recorded task», state runningBot, Database answers (CAP#1)."""
    text = "Сделать поверку счётчиков"
    app = serve_open_bot(launch_app, monkeypatch, database_url)
    delivery = deliver(app, fake_api, text_update(ANNA, text))

    assert delivery.reply_text == SAVED_REPLY, (
        f"Анна's plain text was answered {delivery.reply_text!r}; expected «{SAVED_REPLY}», as SK#2 says"
    )

    tasks = wait_for_tasks(database_url, 1)
    assert [(row["user_id"], row["text"], row["status"]) for row in tasks] == [(ANNA.id, text, RECORDED)], (
        f"expected one task «{text}» under {ANNA.id} with status {RECORDED!r}; the table tasks:\n"
        + describe_tasks(tasks)
    )
    assert_created_now(tasks[0], delivery.sent_at_wall)

    inbox = wait_for_inbox_rows(database_url, 1)
    assert [(row["user_id"], row["kind"], row["text"]) for row in inbox] == [(ANNA.id, "text", text)], (
        f"the message must still be one inbox item «{text}» of kind text under {ANNA.id}; the inbox:\n"
        + describe_inbox(inbox)
    )

    recorded = wait_for_records(app, event="task_recorded")
    assert recorded, "no stdout line with event=task_recorded for the recorded task; stdout was:\n" + "\n".join(
        app.stdout_lines
    )
    assert all(record.get("version") == APP_VERSION for record in recorded), (
        f"the event=task_recorded lines must carry version={APP_VERSION!r}: {recorded!r}"
    )


def test_cap1_command_is_not_a_task(launch_app, monkeypatch, fake_api, database_url):
    """S02 «command is not a task», state runningBot, Database answers (CAP#1, rule becomes-task/command)."""
    app = serve_open_bot(launch_app, monkeypatch, database_url)
    delivery = deliver(app, fake_api, command_update(ANNA, "/help"))

    assert delivery.reply_text == SAVED_REPLY, f"/help was answered {delivery.reply_text!r}; expected «{SAVED_REPLY}»"
    inbox = wait_for_inbox_rows(database_url, 1)
    assert [(row["user_id"], row["text"]) for row in inbox] == [(ANNA.id, "/help")], (
        "/help must stay an inbox item; the inbox:\n" + describe_inbox(inbox)
    )

    settle(delivery.reply.received_at)
    assert_no_task(database_url, "the command /help")


def test_cap1_voice_note_is_not_a_task(launch_app, monkeypatch, fake_api, database_url):
    """S02 «voice note is not a task», state runningBot, Database answers (CAP#1, rule becomes-task/voice)."""
    app = serve_open_bot(launch_app, monkeypatch, database_url)
    delivery = deliver(app, fake_api, voice_update(ANNA))

    inbox = wait_for_inbox_rows(database_url, 1)
    assert [(row["user_id"], row["kind"]) for row in inbox] == [(ANNA.id, "voice")], (
        "the voice note must stay an inbox item of kind voice; the inbox:\n" + describe_inbox(inbox)
    )

    settle(delivery.reply.received_at)
    assert_no_task(database_url, "a voice note")


def test_cap1_photo_is_not_a_task(launch_app, monkeypatch, fake_api, database_url):
    """S02 «photo is not a task» (a photo with a caption), state runningBot, Database answers (CAP#1, rule
    becomes-task/photo)."""
    app = serve_open_bot(launch_app, monkeypatch, database_url)
    delivery = deliver(app, fake_api, photo_update(ANNA, photo_sizes(), caption="Чек из аптеки — вернуть до пятницы"))

    settle(delivery.reply.received_at)
    assert_no_task(database_url, "a photo, even with a caption,")


def test_cap1_sticker_is_not_a_task(launch_app, monkeypatch, fake_api, database_url):
    """S02 «sticker is not a task», state runningBot, Database answers (CAP#1, rule becomes-task/anything else)."""
    app = serve_open_bot(launch_app, monkeypatch, database_url)
    delivery = deliver(app, fake_api, sticker_update(ANNA))

    settle(delivery.reply.received_at)
    assert_no_task(database_url, "a sticker")


def test_cap1_message_with_several_things_is_one_task(launch_app, monkeypatch, fake_api, database_url):
    """S02 «message with several things is one task», state runningBot, Database answers (CAP#1)."""
    text = "Купить молоко\nПозвонить маме"
    app = serve_open_bot(launch_app, monkeypatch, database_url)
    delivery = deliver(app, fake_api, text_update(ANNA, text))

    settle(delivery.reply.received_at)  # a second task, if any, has come by then
    tasks = wait_for_tasks(database_url, 1)
    assert [(row["user_id"], row["text"], row["status"]) for row in tasks] == [(ANNA.id, text, RECORDED)], (
        f"a message with several things must be one task {text!r} under {ANNA.id} with status {RECORDED!r}; "
        "the table tasks:\n" + describe_tasks(tasks)
    )


def test_cap1_repeated_update_makes_no_second_task(launch_app, monkeypatch, fake_api, database_url):
    """S02 «repeated update makes no second task», state borisUpdateComesAgain, Database answers (CAP#1, DUP#1)."""
    text = "Забрать посылку"
    app = serve_open_bot(launch_app, monkeypatch, database_url)

    # borisSavedAMessage: Борис sent «Забрать посылку».
    update = text_update(BORIS, text)
    delivery = deliver(app, fake_api, update)
    assert delivery.reply_text == SAVED_REPLY, f"given borisSavedAMessage: the reply was {delivery.reply_text!r}"

    # borisUpdateComesAgain: Telegram got no answer and sends that very update again.
    since = fake_api.mark()
    answered_at = post_again(app, update)
    wait_until_repeat_handled(app, fake_api, database_url, user=BORIS, text=text, since=since, answered_at=answered_at)

    tasks = fetch_tasks(database_url)
    assert [(row["user_id"], row["text"]) for row in tasks] == [(BORIS.id, text)], (
        f"«{text}» must be one task under {BORIS.id}, also after Telegram sent the update again; the table tasks:\n"
        + describe_tasks(tasks)
        + "\nthe inbox:\n"
        + describe_inbox(fetch_inbox(database_url))
    )
