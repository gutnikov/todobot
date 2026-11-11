"""DUP#1 One inbox item per message: Telegram's repeat of an update is kept once and not replied to again,
while a new message with the same text is a new inbox item. Slice S02's examples in
agentify/specs/requirements/DUP.yaml.

"The same update again" is the same Update JSON posted twice (same update_id, chat and message_id);
a new message has a message_id (and an update_id) of its own.
"""

from __future__ import annotations

from typing import Any

import pytest
from dup_harness import BORIS, count_items, describe_inbox, post_again, wait_until_repeat_handled
from harness import APP_VERSION, SAVED_REPLY, deliver, fetch_inbox, find_records, text_update, wait_for_inbox_rows

pytestmark = pytest.mark.acceptance

TEXT = "Забрать посылку"


def _boris_saved_a_message(app, fake_api, database_url) -> dict[str, Any]:
    """State borisSavedAMessage: Борис (402117993) sent «Забрать посылку»; it is one inbox item under his id."""
    update = text_update(BORIS, TEXT)
    delivery = deliver(app, fake_api, update)
    assert delivery.reply_text == SAVED_REPLY, f"given borisSavedAMessage: the reply was {delivery.reply_text!r}"
    rows = wait_for_inbox_rows(database_url, 1)
    assert count_items(rows, BORIS.id, TEXT) == 1, (
        f"given borisSavedAMessage: expected one inbox item «{TEXT}» under {BORIS.id}; the inbox:\n"
        + describe_inbox(rows)
    )
    return update


def test_dup1_repeated_update_is_kept_once_and_not_replied_to_again(launch_app, fake_api, database_url):
    """S02 «repeated update is kept once and not replied to again», state borisUpdateComesAgain (DUP#1)."""
    app = launch_app(database_url)
    update = _boris_saved_a_message(app, fake_api, database_url)

    # borisUpdateComesAgain: Telegram got no answer and sends that very update again.
    since = fake_api.mark()
    answered_at = post_again(app, update)
    wait_until_repeat_handled(app, fake_api, database_url, user=BORIS, text=TEXT, since=since, answered_at=answered_at)

    rows = fetch_inbox(database_url)
    assert count_items(rows, BORIS.id, TEXT) == 1, (
        f"«{TEXT}» is stored {count_items(rows, BORIS.id, TEXT)} times under {BORIS.id}; the same update again "
        f"must stay one inbox item. The inbox:\n" + describe_inbox(rows)
    )

    replies = [call.params.get("text") for call in fake_api.send_messages(BORIS.id)]
    assert replies == [SAVED_REPLY], (
        f"Борис's chat got {replies!r}; expected «{SAVED_REPLY}» once, nothing for the repeated update.\n"
        f"Bot API calls:\n{fake_api.describe_calls()}"
    )

    lines = app.stop()
    repeats = find_records(lines, event="inbox_repeat")
    assert repeats, "no stdout line with event=inbox_repeat for the repeated update; stdout was:\n" + "\n".join(lines)
    assert all(record.get("version") == APP_VERSION for record in repeats), (
        f"the event=inbox_repeat lines must carry version={APP_VERSION!r}: {repeats!r}"
    )


def test_dup1_new_message_with_the_same_text_is_saved_again(launch_app, fake_api, database_url):
    """S02 «new message with the same text is saved again», state borisSavedAMessage (DUP#1)."""
    app = launch_app(database_url)
    _boris_saved_a_message(app, fake_api, database_url)

    # A new message: text_update gives it a message_id and an update_id of its own.
    delivery = deliver(app, fake_api, text_update(BORIS, TEXT))

    rows = wait_for_inbox_rows(database_url, 2)
    assert count_items(rows, BORIS.id, TEXT) == 2, (
        f"«{TEXT}» is stored {count_items(rows, BORIS.id, TEXT)} times under {BORIS.id}; a new message with the "
        f"same text must be a new inbox item (twice in all). The inbox:\n" + describe_inbox(rows)
    )
    assert delivery.reply_text == SAVED_REPLY
