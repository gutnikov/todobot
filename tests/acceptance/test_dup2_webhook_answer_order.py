"""DUP#2 No message lost: the webhook answers Telegram only after the update is handled, that is the message
stored and the reply sent, or, when storing fails, the apology tried. Slice S02's examples in
agentify/specs/requirements/DUP.yaml.

The order is made visible by the seam's fake Bot API, which holds its answer to every sendMessage
SEND_MESSAGE_HOLD s: the moment each sendMessage call reached the fake is compared with the moment the webhook's 200
arrived, and the inbox is read the moment the 200 arrives.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from dup_harness import (
    ANNA,
    GIVE_UP_AFTER,
    SEND_MESSAGE_HOLD,
    HoldingBotApi,
    count_items,
    describe_inbox,
    post_update_timed,
)
from harness import ERROR_REPLY, SAVED_REPLY, WEBHOOK_PATH, text_update

pytestmark = pytest.mark.acceptance

TEXT = "Купить молоко и хлеб"


@pytest.fixture
def fake_api() -> Iterator[HoldingBotApi]:
    """The Bot API for this module (it overrides conftest's, so launch_app uses it too): sendMessage is held."""
    api = HoldingBotApi(hold=SEND_MESSAGE_HOLD).start()
    try:
        yield api
    finally:
        api.stop()


def test_dup2_webhook_answers_after_the_message_is_stored_and_the_reply_sent(launch_app, fake_api, database_url):
    """S02 «webhook answers after the message is stored and the reply sent», state runningBot (DUP#2)."""
    app = launch_app(database_url)

    since = fake_api.mark()
    answer = post_update_timed(app, text_update(ANNA, TEXT), read_inbox_of=database_url)
    assert answer.response.status_code == 200, f"POST {WEBHOOK_PATH} answered {answer.response.status_code}"
    answered_after = answer.answered_at - answer.posted_at

    assert answer.inbox is not None
    assert count_items(answer.inbox, ANNA.id, TEXT) == 1, (
        f"the webhook answered 200 {answered_after:.3f} s after the POST, but «{TEXT}» was not stored under "
        f"{ANNA.id} by then: it must answer only after the message is stored. The inbox at the 200:\n"
        + describe_inbox(answer.inbox)
    )

    reply = fake_api.wait_for_send_message(ANNA.id, since=since, deadline=answer.posted_at + GIVE_UP_AFTER)
    assert reply is not None, (
        f"no sendMessage to Анна's chat {ANNA.id} reached the Bot API within {GIVE_UP_AFTER:g} s\n"
        f"Bot API calls:\n{fake_api.describe_calls()}"
    )
    assert reply.params.get("text") == SAVED_REPLY
    assert reply.received_at < answer.answered_at, (
        f"the webhook answered 200 {answered_after:.3f} s after the POST, but «{SAVED_REPLY}» reached the Bot API "
        f"only {reply.received_at - answer.posted_at:.3f} s after it: the webhook must answer only after the reply "
        f"is sent"
    )


def test_dup2_webhook_answers_after_the_apology_when_storing_fails(launch_app, fake_api, database_url):
    """S02 «webhook answers after the apology when storing fails», state runningBot, Database: storingFails (DUP#2)."""
    app = launch_app(database_url, migrate=False)  # SK-5's trigger: storing a message fails

    since = fake_api.mark()
    answer = post_update_timed(app, text_update(ANNA, TEXT))
    assert answer.response.status_code == 200, f"POST {WEBHOOK_PATH} answered {answer.response.status_code}"
    answered_after = answer.answered_at - answer.posted_at

    reply = fake_api.wait_for_send_message(ANNA.id, since=since, deadline=answer.posted_at + GIVE_UP_AFTER)
    assert reply is not None, (
        f"no sendMessage to Анна's chat {ANNA.id} reached the Bot API within {GIVE_UP_AFTER:g} s\n"
        f"Bot API calls:\n{fake_api.describe_calls()}"
    )
    assert reply.params.get("text") == ERROR_REPLY
    assert reply.received_at < answer.answered_at, (
        f"the webhook answered 200 {answered_after:.3f} s after the POST, but the apology reached the Bot API "
        f"only {reply.received_at - answer.posted_at:.3f} s after it: the webhook must answer only after the "
        f"apology is tried"
    )
