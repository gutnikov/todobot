"""TRL#1 `/trello` replies «Чтобы подключить Trello, открой ссылку и разреши доступ: <link>», the link being Trello's
authorize page asking for read and write access with no end date (OAuth 1.0a: `scope=read,write`,
`expiration=never`). When Анна allows it, the browser shows «Trello подключён, можно возвращаться в Telegram.» and her
chat gets exactly «Trello подключён ✅ Задачи будут на доске «todobot».»; when she refuses, the browser shows «Trello не
подключён.» and her chat gets exactly «Trello не подключён: доступ не разрешён.». Slices S06's and S07's examples whose
first requirement is TRL#1, in agentify/specs/requirements/TRL.yaml (the model's export).

States, reached by driving the bot (trl_harness): runningBotWithTrello (`serve` with TRL's environment and Trello at
the fake; Анна, 5812345678, has not connected Trello) and annaAskedForTrello (Анна sent /trello and her browser opened
the link). Her decision is a button on the fake Trello's page: Allow sends the browser to the bot's callback with
`oauth_token` and `oauth_verifier`, Deny with `oauth_token` and no `oauth_verifier`.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from acc_harness import wait_for_records
from harness import APP_VERSION
from trl_harness import (
    BOARD_NAME,
    CONNECTED_PAGE,
    CONNECTED_REPLY,
    DECLINED_PAGE,
    DECLINED_REPLY,
    STATUS_LISTS,
    running_bot_with_trello,
    trello_flag,
)

pytestmark = pytest.mark.acceptance

AUTHORIZE_PATH = "/1/OAuthAuthorizeToken"


def test_trl1_trello_replies_with_an_authorization_link(fake_api, database_url):
    """S06 «/trello replies with an authorization link», state runningBotWithTrello (TRL#1)."""
    with running_bot_with_trello(database_url, fake_api) as bot:
        # The reply must be «Чтобы подключить Trello, открой ссылку и разреши доступ: <link>».
        link = bot.send_trello_command()

        parts = urlsplit(link)
        assert f"{parts.scheme}://{parts.netloc}" == bot.trello.base_url, (
            f"the link {link} must lead to Trello's authorize page at TRELLO_AUTH_BASE ({bot.trello.base_url})"
        )
        assert parts.path.rstrip("/") == AUTHORIZE_PATH, (
            f"the link {link} must be Trello's authorize page {bot.trello.base_url}{AUTHORIZE_PATH}"
        )
        visit = bot.open_link(link)  # it reaches Trello's page with a request token Trello issued to the bot
        assert visit.query.get("scope") == "read,write", (
            f"the authorize page was asked for scope={visit.query.get('scope')!r}; TRL#1 asks for read and write "
            f"access: scope=read,write (the query was {visit.query!r})"
        )
        assert visit.query.get("expiration") == "never", (
            f"the authorize page was asked for expiration={visit.query.get('expiration')!r}; TRL#1 asks for access "
            f"with no end date: expiration=never (the query was {visit.query!r})"
        )


def test_trl1_allowing_access_connects_trello_and_creates_the_board(fake_api, database_url):
    """S07 «allowing access connects Trello and creates the board», state annaAskedForTrello, decision allow, Trello
    takes (TRL#1, TRL#2; the rows of the rule status-list)."""
    with running_bot_with_trello(database_url, fake_api) as bot:
        visit = bot.ask_for_trello()

        callback = bot.decide(visit, allow=True)

        shown = (
            f"after Анна allowed access, her browser shows {callback.text!r} (HTTP {callback.status}) at "
            f"{callback.url}; expected «{CONNECTED_PAGE}»"
        )
        assert CONNECTED_PAGE in callback.text, shown
        assert DECLINED_PAGE not in callback.text, shown
        chat = bot.chat_after(callback)
        assert chat == [CONNECTED_REPLY], (
            f"after Анна allowed access, her chat got {chat!r}; expected exactly one message «{CONNECTED_REPLY}»"
        )

        bot.wait_for_board_ready()
        boards = bot.trello.boards_named(BOARD_NAME, include_closed=True)
        assert len(boards) == 1, (
            f"Анна's Trello must have one board «{BOARD_NAME}» (she had none, so the bot creates it); it has "
            f"{len(boards)}:\n{bot.trello.describe_state()}"
        )
        lists = [x.name for x in bot.trello.lists_of(boards[0].id)]
        assert lists == list(STATUS_LISTS), (
            f"the board «{BOARD_NAME}» has the lists {lists!r}; expected exactly {list(STATUS_LISTS)!r}, in this order"
        )
        default_lists = [request.params.get("defaultLists") for request in bot.trello.board_creations()]
        assert [trello_flag(value) for value in default_lists] == [False], (
            f"the board was created with defaultLists={default_lists!r}: Trello gives a new board its own lists "
            "«To Do», «Doing», «Done» unless defaultLists=false, and the board must have the five lists only"
        )

        connected = wait_for_records(bot.app, event="trello_connected")
        assert connected, "no stdout line with event=trello_connected after Анна connected Trello"
        assert all(record.get("version") == APP_VERSION for record in connected), (
            f"the event=trello_connected lines must carry version={APP_VERSION!r}: {connected!r}"
        )


def test_trl1_refusing_access_leaves_trello_not_connected(fake_api, database_url):
    """S07 «refusing access leaves Trello not connected», state annaAskedForTrello, decision deny, Trello takes
    (TRL#1)."""
    with running_bot_with_trello(database_url, fake_api) as bot:
        visit = bot.ask_for_trello()

        callback = bot.decide(visit, allow=False)

        shown = (
            f"after Анна refused access, her browser shows {callback.text!r} (HTTP {callback.status}) at "
            f"{callback.url}; expected «{DECLINED_PAGE}»"
        )
        assert DECLINED_PAGE in callback.text, shown
        assert CONNECTED_PAGE not in callback.text, shown
        chat = bot.chat_after(callback)
        assert chat == [DECLINED_REPLY], (
            f"after Анна refused access, her chat got {chat!r}; expected exactly one message «{DECLINED_REPLY}»"
        )
        assert bot.trello.access_tokens_issued == 0, "Trello issued an access token although Анна refused access"
        assert bot.trello.boards == [], (
            f"Trello is not connected, yet Анна's Trello has boards:\n{bot.trello.describe_state()}"
        )
