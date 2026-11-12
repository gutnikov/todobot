"""TRL#2 Once connected, the bot uses Анна's board «todobot», creating it when there is none. The board has five lists,
in this order, one per status: «Записана», «Исследуется», «Ждёт ответов», «План готов», «Сделана». A list that is
missing is added; nothing else on the board is changed. Slice S07's example whose first requirement is TRL#2, in
agentify/specs/requirements/TRL.yaml (the model's export); the board made from nothing is pinned by
test_trl1_allowing_access_connects_trello_and_creates_the_board.

State annaHasBoardWithTwoLists, reached by driving the bot (trl_harness): as annaAskedForTrello (Анна sent /trello and
her browser opened the link), and Анна's Trello already has a board «todobot» with her own lists «Записана» and «Идеи»
and nothing else, set up in the fake Trello as Trello's own state.
"""

from __future__ import annotations

from collections import Counter

import pytest
from dup_harness import QUIET_PERIOD
from trl_harness import BOARD_NAME, STATUS_LISTS, running_bot_with_trello, settle

pytestmark = pytest.mark.acceptance

ANNAS_OWN_LISTS = ["Записана", "Идеи"]


def test_trl2_an_existing_todobot_board_is_reused_and_its_missing_lists_added(fake_api, database_url):
    """S07 «an existing todobot board is reused and its missing lists added», state annaHasBoardWithTwoLists, decision
    allow, Trello takes (TRL#2)."""
    with running_bot_with_trello(database_url, fake_api) as bot:
        own = bot.trello.add_board(BOARD_NAME, ANNAS_OWN_LISTS)
        own_lists = bot.trello.lists_of(own.id)
        visit = bot.ask_for_trello()

        bot.decide(visit, allow=True)

        ready = bot.wait_for_board_ready()
        assert ready is not None, (
            f"within 10 s of Анна allowing access, no board «{BOARD_NAME}» of hers had all of the lists "
            f"{', '.join(STATUS_LISTS)}:\n{bot.trello.describe_state()}"
        )
        settle(QUIET_PERIOD)  # a second board, or a list added twice, would show now

        boards = [(board.id, board.name, board.closed) for board in bot.trello.boards]
        assert boards == [(own.id, BOARD_NAME, False)], (
            f"Анна's own board «{BOARD_NAME}» must be used: one board, not a second, and nothing else made or changed "
            f"in her Trello; her Trello now:\n{bot.trello.describe_state()}"
        )

        lists = bot.trello.lists_of(own.id, include_closed=True)
        now = {trello_list.id: trello_list for trello_list in lists}
        for before in own_lists:
            assert now.get(before.id) == before, (
                f"Анна's list «{before.name}» must be kept as it was ({before!r}); it is now {now.get(before.id)!r}"
            )
        names = [trello_list.name for trello_list in lists if not trello_list.closed]
        expected = [*ANNAS_OWN_LISTS, *STATUS_LISTS[1:]]
        assert Counter(names) == Counter(expected), (
            f"the board's lists are {names!r}; expected «Записана» and «Идеи» kept and «Исследуется», «Ждёт ответов», "
            "«План готов», «Сделана» added, each list once"
        )
        statuses = [name for name in names if name in STATUS_LISTS]
        assert statuses == list(STATUS_LISTS), (
            f"the board's status lists come in the order {statuses!r}; TRL#2 orders them {list(STATUS_LISTS)!r} "
            f"(all lists: {names!r})"
        )
