"""TRL#1 in the running app: `/trello` replies with Trello's authorize link; Trello's callback connects the user who
asked (allow) or not (deny), once, and not after the request expired; `/trello` again replaces the link and the access.
Nothing secret reaches the logs: no token, no verifier, no secret, no link. Without TRELLO_API_KEY there is none of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import psycopg
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from fakes import (
    APP_SECRET_KEY,
    HEADER,
    SECRET,
    FakeBotApi,
    eventually,
    logs_on_stdout,
    message_update,
    running_app,
    trello_settings,
)
from trello_fake import KEY, FakeTrello, callback_path, query_of

from todobot import texts
from todobot.crypto import SecretBox
from todobot.trello.api import Access
from todobot.web import WEBHOOK_PATH, AccessLog

ANNA = 5_812_345_678
BORIS = 402_117_993
LINK_PREFIX = texts.TRELLO_LINK.format(link="")
CALLBACK = "/trello/callback"

Client = TestClient[Any, Any]


def command(text: str) -> dict[str, Any]:
    return {"text": text, "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}


def records(stdout: str, event: str) -> list[dict[str, Any]]:
    return [record for line in stdout.splitlines() if (record := json.loads(line))["event"] == event]


def run_app(
    database_url: str,
    steps: Callable[[Client, FakeBotApi, FakeTrello], Awaitable[Any]],
    trello: FakeTrello | None = None,
) -> Any:
    trello = trello or FakeTrello()

    async def run() -> Any:
        async with (
            TestServer(trello.app) as trello_server,
            running_app(database_url, trello=trello_settings(trello_server)) as (client, api),
        ):
            return await steps(client, api, trello)

    return asyncio.run(run())


async def post(client: Client, user_id: int, **content: Any) -> None:
    response = await client.post(WEBHOOK_PATH, json=message_update(user_id, **content), headers={HEADER: SECRET})
    assert response.status == 200


async def ask_for_link(client: Client, api: FakeBotApi, user_id: int = ANNA) -> str:
    """The user sends /trello; the link from the reply."""
    since = len(api.calls)
    await post(client, user_id, **command("/trello"))
    [(method, form)] = api.calls[since:]
    assert method == "sendMessage"
    assert form["text"].startswith(LINK_PREFIX), form["text"]
    return str(form["text"][len(LINK_PREFIX) :])


async def open_callback(client: Client, url: str) -> tuple[int, str, dict[str, str]]:
    response = await client.get(callback_path(url))
    return response.status, await response.text(), dict(response.headers)


def rows(database_url: str, query: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(database_url) as conn:
        return conn.execute(query).fetchall()


def sent(api: FakeBotApi, since: int = 0) -> list[tuple[str, str]]:
    return [(form["chat_id"], form["text"]) for method, form in api.calls[since:] if method == "sendMessage"]


# --- /trello ----------------------------------------------------------------------------------------------------------


def test_trello_replies_with_the_link_and_keeps_the_request_for_its_user(migrated_database_url, capsys) -> None:
    async def steps(client: Client, api: FakeBotApi, trello: FakeTrello) -> tuple[str, dict[str, str]]:
        link = await ask_for_link(client, api)
        return link, api.calls[-1][1]

    with logs_on_stdout():
        link, form = run_app(migrated_database_url, steps)
    stdout = capsys.readouterr().out

    query = query_of(link)
    assert urlsplit(link).path == "/1/OAuthAuthorizeToken"
    assert (query["scope"], query["expiration"], query["name"]) == ("read,write", "never", "todobot")
    assert json.loads(form["link_preview_options"]) == {"is_disabled": True}
    [(token, sealed, user_id, expires_at)] = rows(
        migrated_database_url, "SELECT token, secret, user_id, expires_at FROM trello_requests"
    )
    assert (token, user_id) == (query["oauth_token"], ANNA)
    assert SecretBox(APP_SECRET_KEY).open(sealed) is not None  # sealed, not in the clear
    assert sealed != SecretBox(APP_SECRET_KEY).open(sealed)
    assert timedelta(minutes=29) < expires_at - datetime.now(UTC) <= timedelta(minutes=30)
    assert rows(migrated_database_url, "SELECT count(*) FROM inbox") == [(0,)]  # /trello is no inbox item
    [linked] = records(stdout, "trello_link_sent")
    assert linked["user_id"] == ANNA
    assert token not in stdout


# --- the callback -----------------------------------------------------------------------------------------------------


def test_allowing_access_connects_the_user_who_asked_and_sets_up_the_board(migrated_database_url, capsys) -> None:
    async def steps(client: Client, api: FakeBotApi, trello: FakeTrello) -> tuple[Any, ...]:
        link = await ask_for_link(client, api)
        request_token = query_of(link)["oauth_token"]
        request_secret = trello.request_tokens[request_token].secret
        url = trello.decide(request_token, allow=True)
        since = len(api.calls)
        page = await open_callback(client, url)
        await eventually(lambda: trello.boards and len(trello.lists) == len(texts.STATUS_LISTS))
        return page, sent(api, since), trello, (request_token, request_secret, query_of(url)["oauth_verifier"], link)

    with logs_on_stdout():
        (status, body, headers), chat, trello, secrets = run_app(migrated_database_url, steps)
    stdout = capsys.readouterr().out

    assert status == 200
    assert texts.TRELLO_CONNECTED_PAGE in body
    assert texts.TRELLO_DECLINED_PAGE not in body
    assert (headers["Cache-Control"], headers["Referrer-Policy"]) == ("no-store", "no-referrer")
    assert chat == [(str(ANNA), texts.TRELLO_CONNECTED)]
    [(user_id, sealed)] = rows(migrated_database_url, "SELECT user_id, access FROM trello_connections")
    access = Access.loads(SecretBox(APP_SECRET_KEY).open(sealed) or "")
    assert user_id == ANNA
    assert access is not None
    assert trello.access_tokens == {access.token: access.secret}
    assert access.token not in sealed
    assert rows(migrated_database_url, "SELECT count(*) FROM trello_requests") == [(0,)]
    assert [b["name"] for b in trello.boards] == ["todobot"]
    [connected] = records(stdout, "trello_connected")
    assert (connected["level"], connected["user_id"]) == ("INFO", ANNA)
    # nothing secret in the logs, and the callback's access line without its query
    for secret in (*secrets, access.token, access.secret, APP_SECRET_KEY, KEY):
        assert secret not in stdout
    callbacks = [r for r in records(stdout, "log") if r.get("logger") == "aiohttp.access" and r.get("path") == CALLBACK]
    assert [(r["method"], r["status"], r["message"]) for r in callbacks] == [("GET", 200, f"GET {CALLBACK} 200")]


def test_refusing_access_leaves_trello_not_connected(migrated_database_url, capsys) -> None:
    async def steps(client: Client, api: FakeBotApi, trello: FakeTrello) -> tuple[Any, ...]:
        link = await ask_for_link(client, api)
        url = trello.decide(query_of(link)["oauth_token"], allow=False)
        since = len(api.calls)
        page = await open_callback(client, url)
        await asyncio.sleep(0.2)  # a board set up now would show
        return page, sent(api, since), trello

    with logs_on_stdout():
        (status, body, _), chat, trello = run_app(migrated_database_url, steps)
    stdout = capsys.readouterr().out

    assert status == 200
    assert texts.TRELLO_DECLINED_PAGE in body
    assert chat == [(str(ANNA), texts.TRELLO_DECLINED)]
    assert rows(migrated_database_url, "SELECT count(*) FROM trello_connections") == [(0,)]
    assert rows(migrated_database_url, "SELECT count(*) FROM trello_requests") == [(0,)]
    assert trello.access_tokens == {}
    assert trello.boards == []
    assert [r["user_id"] for r in records(stdout, "trello_declined")] == [ANNA]


def test_a_request_token_the_bot_did_not_get_connects_no_one(migrated_database_url, capsys) -> None:
    async def steps(client: Client, api: FakeBotApi, trello: FakeTrello) -> list[int]:
        statuses = []
        for path in (f"{CALLBACK}?oauth_token=guessed&oauth_verifier=v", f"{CALLBACK}?oauth_verifier=v", CALLBACK):
            response = await client.get(path)
            assert texts.TRELLO_DECLINED_PAGE in await response.text()
            statuses.append(response.status)
        return statuses

    with logs_on_stdout():
        statuses = run_app(migrated_database_url, steps)
    stdout = capsys.readouterr().out

    assert statuses == [400, 400, 400]
    assert rows(migrated_database_url, "SELECT count(*) FROM trello_connections") == [(0,)]
    assert [r["reason"] for r in records(stdout, "trello_callback_unknown")] == ["unknown"] * 3
    assert "guessed" not in stdout


def test_an_expired_request_connects_no_one(migrated_database_url, capsys) -> None:
    async def steps(client: Client, api: FakeBotApi, trello: FakeTrello) -> tuple[Any, ...]:
        link = await ask_for_link(client, api)
        with psycopg.connect(migrated_database_url, autocommit=True) as conn:
            conn.execute("UPDATE trello_requests SET expires_at = now() - interval '1 second'")
        url = trello.decide(query_of(link)["oauth_token"], allow=True)
        since = len(api.calls)
        return await open_callback(client, url), sent(api, since), trello

    with logs_on_stdout():
        (status, body, _), chat, trello = run_app(migrated_database_url, steps)
    stdout = capsys.readouterr().out

    assert status == 400
    assert texts.TRELLO_DECLINED_PAGE in body
    assert chat == []
    assert trello.access_tokens == {}  # Trello was not even asked
    assert rows(migrated_database_url, "SELECT count(*) FROM trello_requests") == [(0,)]
    assert [r["reason"] for r in records(stdout, "trello_callback_unknown")] == ["expired"]


def test_a_callback_serves_once_and_never_on_head(migrated_database_url) -> None:
    async def steps(client: Client, api: FakeBotApi, trello: FakeTrello) -> list[int]:
        link = await ask_for_link(client, api)
        url = trello.decide(query_of(link)["oauth_token"], allow=True)
        head = await client.head(callback_path(url))
        first = await client.get(callback_path(url))
        again = await client.get(callback_path(url))
        return [head.status, first.status, again.status]

    assert run_app(migrated_database_url, steps) == [405, 200, 400]
    assert rows(migrated_database_url, "SELECT user_id FROM trello_connections") == [(ANNA,)]


def test_trello_again_replaces_the_earlier_link_and_then_the_access(migrated_database_url) -> None:
    async def steps(client: Client, api: FakeBotApi, trello: FakeTrello) -> list[Any]:
        first = query_of(await ask_for_link(client, api))["oauth_token"]
        second = query_of(await ask_for_link(client, api))["oauth_token"]
        statuses = [
            (await open_callback(client, trello.decide(first, allow=True)))[0],
            (await open_callback(client, trello.decide(second, allow=True)))[0],
        ]
        [(before,)] = rows(migrated_database_url, "SELECT access FROM trello_connections")
        third = query_of(await ask_for_link(client, api))["oauth_token"]
        statuses.append((await open_callback(client, trello.decide(third, allow=True)))[0])
        [(after,)] = rows(migrated_database_url, "SELECT access FROM trello_connections")
        await eventually(lambda: rows(migrated_database_url, "SELECT list_id FROM trello_connections")[0][0])
        return [statuses, before, after, trello]

    statuses, before, after, trello = run_app(migrated_database_url, steps)

    assert statuses == [400, 200, 200]
    box = SecretBox(APP_SECRET_KEY)
    old, new = Access.loads(box.open(before) or ""), Access.loads(box.open(after) or "")
    assert old is not None
    assert new is not None
    assert old.token != new.token
    assert len(trello.access_tokens) == 2
    assert len(trello.boards) == 1  # set up again with the new access, the same board found


def test_a_user_can_only_connect_through_their_own_request(migrated_database_url) -> None:
    """Борис's callback connects Борис, whoever's browser opens it: the user comes from the request, not the URL."""

    async def steps(client: Client, api: FakeBotApi, trello: FakeTrello) -> list[tuple[str, str]]:
        token = query_of(await ask_for_link(client, api, BORIS))["oauth_token"]
        url = trello.decide(token, allow=True)
        since = len(api.calls)
        await client.get(f"{callback_path(url)}&user_id={ANNA}")
        return sent(api, since)

    assert run_app(migrated_database_url, steps) == [(str(BORIS), texts.TRELLO_CONNECTED)]
    assert rows(migrated_database_url, "SELECT user_id FROM trello_connections") == [(BORIS,)]


def test_when_trello_refuses_the_access_token_the_user_is_told_and_not_connected(migrated_database_url, capsys) -> None:
    async def steps(client: Client, api: FakeBotApi, trello: FakeTrello) -> tuple[Any, ...]:
        link = await ask_for_link(client, api)
        url = trello.decide(query_of(link)["oauth_token"], allow=True)
        since = len(api.calls)
        tampered = url.replace("oauth_verifier=", "oauth_verifier=x")
        return await open_callback(client, tampered), sent(api, since)

    with logs_on_stdout():
        (status, body, _), chat = run_app(migrated_database_url, steps)
    stdout = capsys.readouterr().out

    assert status == 502
    assert texts.TRELLO_DECLINED_PAGE in body
    assert chat == [(str(ANNA), texts.ERROR)]
    assert rows(migrated_database_url, "SELECT count(*) FROM trello_connections") == [(0,)]
    [failed] = records(stdout, "trello_connect_failed")
    assert (failed["level"], failed["error"]) == ("ERROR", "POST /1/OAuthGetAccessToken: HTTP 401")


def test_when_trello_gives_no_request_token_the_user_gets_the_apology(migrated_database_url, capsys) -> None:
    async def steps(client: Client, api: FakeBotApi, trello: FakeTrello) -> list[tuple[str, str]]:
        await post(client, ANNA, **command("/trello"))
        return sent(api)

    with logs_on_stdout():
        chat = run_app(migrated_database_url, steps, trello=FakeTrello(key="not-the-bots-key"))
    stdout = capsys.readouterr().out

    assert chat == [(str(ANNA), texts.ERROR)]
    [error] = records(stdout, "handler_error")
    assert error["error"] == "TrelloError"
    assert "POST /1/OAuthGetRequestToken: HTTP 401" in error["exc"]
    assert rows(migrated_database_url, "SELECT count(*) FROM trello_requests") == [(0,)]


def test_a_task_recorded_through_the_webhook_gets_its_card_at_once(migrated_database_url) -> None:
    """The loop's tick is an hour here: the card comes because the webhook nudged it, not the clock."""

    async def steps(client: Client, api: FakeBotApi, trello: FakeTrello) -> tuple[list[tuple[str, str]], Any]:
        link = await ask_for_link(client, api)
        await open_callback(client, trello.decide(query_of(link)["oauth_token"], allow=True))
        await eventually(lambda: len(trello.lists) == len(texts.STATUS_LISTS))
        since = len(api.calls)
        await post(client, ANNA, text="Забрать посылку")
        return sent(api, since), await eventually(lambda: trello.cards)

    chat, cards = run_app(migrated_database_url, steps)

    assert chat == [(str(ANNA), texts.SAVED)]
    assert [card["name"] for card in cards] == ["Забрать посылку"]


def test_the_access_log_has_neither_the_query_nor_the_referer() -> None:
    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logged.append(record)

    logger = logging.getLogger("test.access")
    logged: list[logging.LogRecord] = []
    handler = Collect()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    referer = "https://trello.com/1/OAuthAuthorizeToken?oauth_token=tok-from-referer"
    request = make_mocked_request(
        "GET", f"{CALLBACK}?oauth_token=tok-in-query&oauth_verifier=ver-in-query", headers={"Referer": referer}
    )
    try:
        AccessLog(logger, "").log(request, web.Response(status=200, text="ok"), 0.0123)
    finally:
        logger.removeHandler(handler)

    [record] = logged
    line = json.dumps({**vars(record), "message": record.getMessage()}, default=str)
    assert record.getMessage() == f"GET {CALLBACK} 200"
    for secret in ("tok-in-query", "ver-in-query", "tok-from-referer", "Referer"):
        assert secret not in line


# --- off unless set up ------------------------------------------------------------------------------------------------


def test_without_trello_trello_is_a_command_like_any_other_and_there_is_no_callback(migrated_database_url) -> None:
    async def steps() -> tuple[list[tuple[str, str]], int]:
        async with running_app(migrated_database_url) as (client, api):
            await post(client, ANNA, **command("/trello"))
            response = await client.get(f"{CALLBACK}?oauth_token=t&oauth_verifier=v")
            return sent(api), response.status

    chat, status = asyncio.run(steps())

    assert chat == [(str(ANNA), texts.SAVED)]
    assert status == 404
    assert rows(migrated_database_url, "SELECT kind, text FROM inbox") == [("text", "/trello")]
