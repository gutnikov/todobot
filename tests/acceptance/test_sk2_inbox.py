"""SK-2 Taking in a message: one `inbox` row per message, its kind, and the "Сохранил ✅" reply."""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg
import pytest

from harness import (
    APP_VERSION,
    INBOX_COLUMNS,
    SAVED_REPLY,
    TelegramUser,
    command_update,
    deliver,
    document_update,
    find_records,
    photo_sizes,
    photo_update,
    sticker_update,
    stored,
    text_update,
    voice_update,
    wait_for_inbox_rows,
)

pytestmark = pytest.mark.acceptance

ANNA = TelegramUser(id=5_812_345_678, first_name="Анна", username="anna_tasks")
BORIS = TelegramUser(id=402_117_993, first_name="Борис")

# received_at is "when the message was received"; allow for clock skew between the test,
# the app and the database server.
CLOCK_SLACK = timedelta(minutes=2)


def _the_one_row(database_url: str) -> dict:
    rows = wait_for_inbox_rows(database_url, 1)
    assert len(rows) == 1, f"expected exactly one inbox row, found {len(rows)}: {rows!r}"
    return rows[0]


def _assert_received_now(row: dict, sent_at: datetime) -> None:
    received_at = row["received_at"]
    assert isinstance(received_at, datetime), f"received_at is {received_at!r}"
    assert received_at.tzinfo is not None, "received_at must be a timestamptz"
    now = datetime.now().astimezone()
    assert sent_at - CLOCK_SLACK <= received_at <= now + CLOCK_SLACK, (
        f"received_at {received_at.isoformat()} is not around the time the message was sent ({sent_at.isoformat()})"
    )


def test_sk2_migrate_creates_inbox_table_with_contract_columns(migrate, database_url):
    migrate(database_url)

    with psycopg.connect(database_url) as conn:
        columns = {
            name: (data_type, nullable)
            for name, data_type, nullable in conn.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'inbox' AND table_schema = current_schema()"
            ).fetchall()
        }

    assert columns, "`migrate` did not create the table inbox"
    assert {name: columns[name][0] for name in INBOX_COLUMNS if name in columns} == {
        "user_id": "bigint",
        "kind": "text",
        "text": "text",
        "file_id": "text",
        "received_at": "timestamp with time zone",
    }
    assert columns["text"][1] == "YES", "inbox.text must allow NULL"
    assert columns["file_id"][1] == "YES", "inbox.file_id must allow NULL"


def test_sk2_text_message_is_saved_as_text_and_acknowledged(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    delivery = deliver(app, fake_api, text_update(ANNA, "Купить молоко и хлеб"))

    assert delivery.reply_text == SAVED_REPLY
    row = _the_one_row(database_url)
    assert stored(row) == {"user_id": ANNA.id, "kind": "text", "text": "Купить молоко и хлеб", "file_id": None}
    _assert_received_now(row, delivery.sent_at_wall)


def test_sk2_other_command_is_saved_as_text(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    delivery = deliver(app, fake_api, command_update(ANNA, "/help"))

    assert delivery.reply_text == SAVED_REPLY
    row = _the_one_row(database_url)
    assert stored(row) == {"user_id": ANNA.id, "kind": "text", "text": "/help", "file_id": None}


def test_sk2_voice_without_caption_is_saved_with_its_file_id(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    update = voice_update(ANNA)
    delivery = deliver(app, fake_api, update)

    assert delivery.reply_text == SAVED_REPLY
    row = _the_one_row(database_url)
    assert stored(row) == {
        "user_id": ANNA.id,
        "kind": "voice",
        "text": None,
        "file_id": update["message"]["voice"]["file_id"],
    }
    _assert_received_now(row, delivery.sent_at_wall)


def test_sk2_voice_with_caption_is_saved_with_the_caption(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    update = voice_update(ANNA, caption="Позвонить маме вечером")
    delivery = deliver(app, fake_api, update)

    assert delivery.reply_text == SAVED_REPLY
    row = _the_one_row(database_url)
    assert stored(row) == {
        "user_id": ANNA.id,
        "kind": "voice",
        "text": "Позвонить маме вечером",
        "file_id": update["message"]["voice"]["file_id"],
    }


def test_sk2_photo_is_saved_with_file_id_of_the_last_photo_size(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    sizes = photo_sizes()
    delivery = deliver(app, fake_api, photo_update(ANNA, sizes, caption="Чек из аптеки — вернуть до пятницы"))

    assert delivery.reply_text == SAVED_REPLY
    row = _the_one_row(database_url)
    assert stored(row) == {
        "user_id": ANNA.id,
        "kind": "photo",
        "text": "Чек из аптеки — вернуть до пятницы",
        "file_id": sizes[-1]["file_id"],
    }
    _assert_received_now(row, delivery.sent_at_wall)


def test_sk2_photo_without_caption_is_saved_with_null_text(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    sizes = photo_sizes()
    delivery = deliver(app, fake_api, photo_update(ANNA, sizes))

    assert delivery.reply_text == SAVED_REPLY
    row = _the_one_row(database_url)
    assert stored(row) == {"user_id": ANNA.id, "kind": "photo", "text": None, "file_id": sizes[-1]["file_id"]}


def test_sk2_sticker_is_saved_as_other_without_text_or_file_id(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    delivery = deliver(app, fake_api, sticker_update(ANNA))

    assert delivery.reply_text == SAVED_REPLY
    row = _the_one_row(database_url)
    assert stored(row) == {"user_id": ANNA.id, "kind": "other", "text": None, "file_id": None}
    _assert_received_now(row, delivery.sent_at_wall)


def test_sk2_document_with_caption_is_saved_as_other_with_the_caption(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    delivery = deliver(app, fake_api, document_update(ANNA, caption="Список на выходные"))

    assert delivery.reply_text == SAVED_REPLY
    row = _the_one_row(database_url)
    assert stored(row) == {"user_id": ANNA.id, "kind": "other", "text": "Список на выходные", "file_id": None}


def test_sk2_messages_of_two_users_are_stored_under_their_own_user_id(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    deliveries = [
        deliver(app, fake_api, text_update(ANNA, "Записаться к стоматологу")),
        deliver(app, fake_api, text_update(BORIS, "Забрать посылку")),
        deliver(app, fake_api, voice_update(BORIS, caption="Про отчёт")),
        deliver(app, fake_api, text_update(ANNA, "Оплатить интернет")),
    ]

    # deliver() only accepts a reply sent to the sender's own chat
    assert [delivery.reply_text for delivery in deliveries] == [SAVED_REPLY] * 4

    rows = wait_for_inbox_rows(database_url, 4)
    by_user: dict[int, list[tuple]] = {}
    for row in rows:
        by_user.setdefault(row["user_id"], []).append((row["kind"], row["text"]))
    assert {user: sorted(items) for user, items in by_user.items()} == {
        ANNA.id: [("text", "Записаться к стоматологу"), ("text", "Оплатить интернет")],
        BORIS.id: [("text", "Забрать посылку"), ("voice", "Про отчёт")],
    }


def test_sk2_inbox_saved_log_line_has_kind_and_version(launch_app, fake_api, database_url):
    app = launch_app(database_url)
    deliver(app, fake_api, text_update(ANNA, "Купить молоко и хлеб"))
    deliver(app, fake_api, photo_update(ANNA, photo_sizes()))
    deliver(app, fake_api, sticker_update(ANNA))
    assert len(wait_for_inbox_rows(database_url, 3)) == 3

    lines = app.stop()

    saved = find_records(lines, event="inbox_saved", version=APP_VERSION)
    assert {record.get("kind") for record in saved} == {"text", "photo", "other"}, (
        f"expected one event=inbox_saved line per message with kind and version={APP_VERSION!r}; stdout was:\n"
        + "\n".join(lines)
    )
