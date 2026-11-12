"""TRL#4 With Trello connected, `/tasks` reads the board: each task's line shows its card's current name and, as its
status, the name of the list the card is in, in lower case; a card Анна renamed shows its new name, a card she
archived or deleted is not shown. A task that has no card yet shows «— ещё не в Trello». When Trello does not answer,
`/tasks` replies exactly «Trello сейчас не отвечает, попробуй позже.». The rest of CAP#2 holds (the header, oldest
first). Slice S05's examples whose requirement is TRL#4, in agentify/specs/requirements/TRL.yaml (the model's export).

States, reached by driving the bot (trl_harness): annaCardsChangedInTrello (Анна connected Trello and recorded
«Сделать поверку счётчиков», «Записаться к стоматологу» and «Купить шины», each a card; then, in Trello, she moved the
first card to «Исследуется», renamed the second «Стоматолог: чистка и восьмёрки» and archived the third: changes made
in the fake Trello), annaRecordedTaskBeforeTrello (with Trello set up, Анна recorded «Сделать поверку счётчиков» before
connecting it) and annaConnectedTrello with Trello refusing (every REST call answers 500).
"""

from __future__ import annotations

import pytest
from cap_harness import ask_for_tasks
from dup_harness import ANNA, GIVE_UP_AFTER
from trl_harness import TRELLO_DOWN_REPLY, running_bot_with_trello

pytestmark = pytest.mark.acceptance

ANNAS_THREE_TASKS = ("Сделать поверку счётчиков", "Записаться к стоматологу", "Купить шины")
RENAMED = "Стоматолог: чистка и восьмёрки"


def test_trl4_tasks_list_shows_trello_s_names_and_lists(fake_api, database_url):
    """S05 «tasks list shows Trello's names and lists», state annaCardsChangedInTrello (TRL#4)."""
    with running_bot_with_trello(database_url, fake_api) as bot:
        bot.connect_trello()
        for text in ANNAS_THREE_TASKS:
            bot.record_task(text)
        cards = bot.wait_for_cards(len(ANNAS_THREE_TASKS))
        by_name = {card.name: card for card in cards}
        assert sorted(by_name) == sorted(ANNAS_THREE_TASKS), (
            f"while reaching the state annaCardsChangedInTrello, each of Анна's three tasks must have become a card "
            f"within {GIVE_UP_AFTER:g} s; the board has {bot.describe_cards(cards)}"
        )
        meters, dentist, tyres = (by_name[text] for text in ANNAS_THREE_TASKS)
        bot.trello.move_card(meters.id, "Исследуется")
        bot.trello.rename_card(dentist.id, RENAMED)
        bot.trello.archive_card(tyres.id)

        answered = ask_for_tasks(bot.app, bot.api, ANNA)

        listed = f"Твои задачи:\n1. Сделать поверку счётчиков — исследуется\n2. {RENAMED} — записана"
        expected = [(str(ANNA.id), listed)]
        assert answered.replies == expected, (
            f"Анна's /tasks was answered {answered.replies!r} as (chat_id, text); expected exactly {expected!r}: the "
            "cards' current names and lists, the archived card not shown"
        )


def test_trl4_task_without_a_card_shows_it_is_not_in_trello_yet(fake_api, database_url):
    """S05 «task without a card shows it is not in Trello yet», state annaRecordedTaskBeforeTrello (TRL#4)."""
    with running_bot_with_trello(database_url, fake_api) as bot:
        bot.record_task("Сделать поверку счётчиков")

        answered = ask_for_tasks(bot.app, bot.api, ANNA)

        expected = [(str(ANNA.id), "Твои задачи:\n1. Сделать поверку счётчиков — ещё не в Trello")]
        assert answered.replies == expected, (
            f"Анна's /tasks was answered {answered.replies!r} as (chat_id, text); expected exactly {expected!r}"
        )


def test_trl4_tasks_list_says_when_trello_does_not_answer(fake_api, database_url):
    """S05 «tasks list says when Trello does not answer», state annaConnectedTrello, Trello refuses (TRL#4)."""
    with running_bot_with_trello(database_url, fake_api) as bot:
        bot.connect_trello()
        bot.trello.refuse()

        answered = ask_for_tasks(bot.app, bot.api, ANNA)

        expected = [(str(ANNA.id), TRELLO_DOWN_REPLY)]
        assert answered.replies == expected, (
            f"Анна's /tasks while Trello answers every call with 500 was answered {answered.replies!r} as "
            f"(chat_id, text); expected exactly {expected!r}"
        )
