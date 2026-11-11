"""ACC#1 With the allow list set, a private-chat message from a user it does not name, /start included, gets exactly
the refusal and nothing else, nothing of it is stored, and a log line with event=access_denied is written; a user the
list names is served as SK and DUP say. Slices S01's and S02's examples in agentify/specs/model.

States: closedBot (`serve` with TELEGRAM_ALLOWED_USER_IDS naming Anna, 5812345678, only; the message is Anna's) and
closedBotStrangerWrites (as closedBot, and the message is Boris's, 402117993, whom the list does not name).
"""

from __future__ import annotations

import pytest
from acc_harness import GIVE_UP_AFTER, OWNER_ONLY, REFUSED_REPLY, describe, post_and_settle, serve_app, wait_for_records
from dup_harness import ANNA, BORIS, describe_inbox
from harness import SAVED_REPLY, command_update, deliver, fetch_inbox, text_update, wait_for_inbox_rows

pytestmark = pytest.mark.acceptance

BORIS_TEXT = "Забрать посылку"
ANNA_TEXT = "Купить молоко и хлеб"


def _assert_only_the_refusal(handled, app, api) -> None:
    """Boris's chat got exactly the refusal and the bot did nothing else: no greeting, no «saved», no other call."""
    expected = [(str(BORIS.id), REFUSED_REPLY)]
    assert handled.replies == expected, (
        f"after Boris's update (he is not on the allow list) the bot sent {handled.replies!r} as (chat_id, text); "
        f"expected exactly {expected!r}: «{REFUSED_REPLY}» in his chat {BORIS.id} and nothing else "
        f"(no greeting, no «{SAVED_REPLY}»)\n{describe(app, api)}"
    )
    assert not handled.other_calls, (
        f"besides the refusal the bot made other Bot API calls: {[call.short() for call in handled.other_calls]!r}\n"
        f"{describe(app, api)}"
    )


def _assert_access_denied_logged(app, api) -> None:
    denied = wait_for_records(app, event="access_denied")
    assert denied, (
        f"no stdout line with event=access_denied within {GIVE_UP_AFTER:g} s for Boris's refused update\n"
        f"{describe(app, api)}"
    )


def test_acc1_stranger_s_start_is_refused(fake_api, database_url):
    """S01 «stranger's /start is refused», state closedBotStrangerWrites (ACC#1, SK#1)."""
    with serve_app(database_url, fake_api, allowed_user_ids=OWNER_ONLY) as app:
        handled = post_and_settle(app, fake_api, command_update(BORIS, "/start"))

        _assert_only_the_refusal(handled, app, fake_api)
        _assert_access_denied_logged(app, fake_api)


def test_acc1_stranger_s_message_is_refused_and_not_stored(fake_api, database_url):
    """S02 «stranger's message is refused and not stored», state closedBotStrangerWrites, Database answers
    (ACC#1)."""
    with serve_app(database_url, fake_api, allowed_user_ids=OWNER_ONLY) as app:
        handled = post_and_settle(app, fake_api, text_update(BORIS, BORIS_TEXT))

        _assert_only_the_refusal(handled, app, fake_api)
        rows = fetch_inbox(database_url)  # after the quiet period: a late store would show by now
        assert not [row for row in rows if row["user_id"] == BORIS.id], (
            f"the inbox has item(s) under {BORIS.id}, who is not on the allow list; nothing of his message may be "
            f"stored. The inbox:\n{describe_inbox(rows)}"
        )
        assert not [row for row in rows if row["text"] == BORIS_TEXT], (
            f"«{BORIS_TEXT}» is in the inbox; nothing of a refused message may be stored. The inbox:\n"
            f"{describe_inbox(rows)}"
        )
        _assert_access_denied_logged(app, fake_api)


def test_acc1_listed_user_s_message_is_saved_as_before(fake_api, database_url):
    """S02 «listed user's message is saved as before», state closedBot, Database answers (ACC#1)."""
    with serve_app(database_url, fake_api, allowed_user_ids=OWNER_ONLY) as app:
        delivery = deliver(app, fake_api, text_update(ANNA, ANNA_TEXT))

        assert delivery.reply_text == SAVED_REPLY, (
            f"Anna, whom the allow list names, got {delivery.reply_text!r}; expected «{SAVED_REPLY}»\n"
            f"{describe(app, fake_api)}"
        )
        rows = wait_for_inbox_rows(database_url, 1)
        assert [(row["user_id"], row["text"]) for row in rows] == [(ANNA.id, ANNA_TEXT)], (
            f"expected one inbox item «{ANNA_TEXT}» under {ANNA.id}; the inbox:\n{describe_inbox(rows)}"
        )
