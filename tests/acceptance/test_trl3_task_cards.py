"""TRL#3 Every recorded task (CAP#1) becomes a card in the list «Записана» of Анна's board «todobot». A one-line text is
the card's name; a text of several lines gives its first line as the name and the whole text as the first block of
the card's description. The reply stays «Сохранил ✅» right away: while Trello does not take the card, or Анна has not
connected Trello yet, the task waits and the bot tries again (every TRELLO_RETRY_SECONDS, 1 s here) until the card is
made. A repeated update makes no second card. Slice A02's examples in agentify/specs/requirements/TRL.yaml (the
model's export).

States, reached by driving the bot (trl_harness): annaConnectedTrello (Анна connected Trello; her board «todobot» has
the five lists and no cards), annaConnectedTrelloUpdateComesAgain (her «Забрать посылку» was recorded and became a card;
Telegram sends that very update again) and annaRecordedTaskBeforeTrello (she recorded «Сделать поверку счётчиков»
before connecting Trello). Trello refusing, then recovering: the fake answers every REST call with 500 until the test
makes it take again.
"""

from __future__ import annotations

import pytest
from acc_harness import wait_for_records
from dup_harness import ANNA, GIVE_UP_AFTER, QUIET_PERIOD, post_again, wait_until_repeat_handled
from harness import APP_VERSION, SAVED_REPLY, deliver, text_update
from trl_harness import RECORDED_LIST, TRELLO_RETRY_SECONDS, running_bot_with_trello, settle, wait_for

pytestmark = pytest.mark.acceptance

METER_CHECK = "Сделать поверку счётчиков"


def test_trl3_recorded_task_becomes_a_card_in(fake_api, database_url):
    """A02 «recorded task becomes a card in Записана», state annaConnectedTrello, Trello takes (TRL#3)."""
    with running_bot_with_trello(database_url, fake_api) as bot:
        bot.connect_trello()

        delivery = deliver(bot.app, bot.api, text_update(ANNA, METER_CHECK))

        assert delivery.reply_text == SAVED_REPLY, (
            f"Анна's task was answered {delivery.reply_text!r}; expected «{SAVED_REPLY}» (TRL#3: the reply stays)"
        )
        cards = bot.wait_for_cards(1)
        assert [card.name for card in cards] == [METER_CHECK], (
            f"within {GIVE_UP_AFTER:g} s Анна's task must become one card «{METER_CHECK}» on her board «todobot»; "
            f"the board has {bot.describe_cards(cards)}"
        )
        card = cards[0]
        assert bot.trello.list_name(card.id_list) == RECORDED_LIST, (
            f"the card «{card.name}» is in the list «{bot.trello.list_name(card.id_list)}»; expected «{RECORDED_LIST}»"
        )
        assert card.desc.strip() == "", (
            f"a one-line task is the card's name only: its description must be empty, it is {card.desc!r}"
        )

        created = wait_for_records(bot.app, event="card_created")
        assert created, "no stdout line with event=card_created after the card was made"
        assert all(record.get("version") == APP_VERSION for record in created), (
            f"the event=card_created lines must carry version={APP_VERSION!r}: {created!r}"
        )


def test_trl3_multi_line_task_gives_its_first_line_as_the_card_s_name(fake_api, database_url):
    """A02 «multi-line task gives its first line as the card's name», state annaConnectedTrello, Trello takes
    (TRL#3)."""
    text = "Купить молоко\nПозвонить маме"
    with running_bot_with_trello(database_url, fake_api) as bot:
        bot.connect_trello()

        bot.record_task(text)

        cards = bot.wait_for_cards(1)
        assert [card.name for card in cards] == ["Купить молоко"], (
            f"the task {text!r} must become one card named after its first line, «Купить молоко»; the board has "
            f"{bot.describe_cards(cards)}"
        )
        assert cards[0].desc.startswith(text), (
            f"the card's description must start with the whole text {text!r}; it is {cards[0].desc!r}"
        )


def test_trl3_card_waits_while_trello_refuses_and_is_made_once_it_takes(fake_api, database_url):
    """A02 «card waits while Trello refuses and is made once it takes», state annaConnectedTrello, Trello recovers
    (TRL#3)."""
    with running_bot_with_trello(database_url, fake_api) as bot:
        bot.connect_trello()
        bot.trello.refuse()
        since = bot.trello.mark()

        # «Сохранил ✅» right away: it comes while Trello still refuses everything
        bot.record_task(METER_CHECK)

        refused = wait_for(lambda: [r for r in bot.trello.requests(since, kind="rest") if r.status == 500])
        assert refused, (
            f"while Trello refused, the bot made no call to it within {GIVE_UP_AFTER:g} s of Анна's task: TRL#3 says "
            f"it tries to make the card, and tries again every TRELLO_RETRY_SECONDS ({TRELLO_RETRY_SECONDS} s here)"
        )

        bot.trello.take()

        cards = bot.wait_for_cards(1)
        made = [(card.name, bot.trello.list_name(card.id_list)) for card in cards]
        assert made == [(METER_CHECK, RECORDED_LIST)], (
            f"within {GIVE_UP_AFTER:g} s of Trello taking calls again, the waiting task must become a card "
            f"«{METER_CHECK}» in «{RECORDED_LIST}» (the bot tries again every {TRELLO_RETRY_SECONDS} s); the board has "
            f"{bot.describe_cards(cards)}"
        )
        settle(QUIET_PERIOD)  # a retry after the card was made would make a second one now
        cards = bot.cards(include_closed=True)
        assert len(cards) == 1, f"the task must become one card, not {len(cards)}: {bot.describe_cards(cards)}"


def test_trl3_task_recorded_before_connecting_becomes_a_card_once_connected(fake_api, database_url):
    """A02 «task recorded before connecting becomes a card once connected», state annaRecordedTaskBeforeTrello, Trello
    takes (TRL#3)."""
    with running_bot_with_trello(database_url, fake_api) as bot:
        bot.record_task(METER_CHECK)  # before Анна connects Trello: the task has no card yet

        bot.connect_trello()

        cards = bot.wait_for_cards(1)
        made = [(card.name, bot.trello.list_name(card.id_list)) for card in cards]
        assert made == [(METER_CHECK, RECORDED_LIST)], (
            f"within {GIVE_UP_AFTER:g} s of Анна connecting Trello, the task she recorded before must become a card "
            f"«{METER_CHECK}» in «{RECORDED_LIST}»; the board has {bot.describe_cards(cards)}"
        )


def test_trl3_repeated_update_makes_no_second_card(fake_api, database_url):
    """A02 «repeated update makes no second card», state annaConnectedTrelloUpdateComesAgain, Trello takes (TRL#3)."""
    text = "Забрать посылку"
    with running_bot_with_trello(database_url, fake_api) as bot:
        bot.connect_trello()
        update = text_update(ANNA, text)
        delivery = deliver(bot.app, bot.api, update)
        assert delivery.reply_text == SAVED_REPLY, f"Анна's «{text}» was answered {delivery.reply_text!r}"
        first = bot.wait_for_cards(1)
        assert [card.name for card in first] == [text], (
            f"before the repeat (state annaConnectedTrelloUpdateComesAgain), «{text}» must have become a card; the "
            f"board has {bot.describe_cards(first)}"
        )

        since = bot.api.mark()
        answered_at = post_again(bot.app, update)  # the very same update: same update_id, chat and message_id
        wait_until_repeat_handled(
            bot.app, bot.api, database_url, user=ANNA, text=text, since=since, answered_at=answered_at
        )
        settle(2 * TRELLO_RETRY_SECONDS)  # and a retry or two of the bot's, which must not make a card either

        cards = [card for card in bot.cards(include_closed=True) if card.name == text]
        assert len(cards) == 1, (
            f"«{text}» must be on one card only after Telegram sent its update again; it is on {len(cards)}: "
            f"{bot.describe_cards(cards)}"
        )
