"""Trello's API as the bot calls it (TRL#1): OAuth 1.0a signatures (checked by the fake's own RFC 5849 code), the
request token with the callback, the authorize link, the access token, the REST calls signed with the user's token, and
failures that name the call only."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import pytest
from aiohttp.test_utils import TestServer
from fakes import PUBLIC_BASE_URL, trello_settings
from trello_fake import FakeTrello, hmac_sha1, parse_authorization, query_of, signature_base

from todobot.config import TrelloSettings
from todobot.trello.api import Access, TrelloApi, TrelloError, oauth_header

# OAuth Core 1.0, Appendix A: the photos example, with its signature.
EXAMPLE = {
    "url": "http://photos.example.net/photos?file=vacation.jpg&size=original",
    "key": "dpf43f3p2l4k3l03",
    "secret": "kd94hf93k423kf44",
    "token": "nnch734d00sl2jdk",
    "token_secret": "pfkkdhi9sl3r4s00",
    "nonce": "kllo9940pd9333jh",
    "timestamp": "1191242096",
}
EXAMPLE_SIGNATURE = "tR3+Ty81lMeYAr/Fid0kMTYa/WM="


def test_the_signature_is_the_one_of_the_oauth_example() -> None:
    header = oauth_header("GET", **EXAMPLE)

    params = parse_authorization(header)
    assert params["oauth_signature"] == EXAMPLE_SIGNATURE
    assert params["oauth_signature_method"] == "HMAC-SHA1"
    assert params["oauth_consumer_key"] == EXAMPLE["key"]
    assert params["oauth_token"] == EXAMPLE["token"]


def test_the_fakes_own_signing_agrees_with_the_example() -> None:
    """The fake checks the bot's signatures with its own RFC 5849 code: that code must be right too."""
    params = [
        ("oauth_consumer_key", EXAMPLE["key"]),
        ("oauth_token", EXAMPLE["token"]),
        ("oauth_signature_method", "HMAC-SHA1"),
        ("oauth_timestamp", EXAMPLE["timestamp"]),
        ("oauth_nonce", EXAMPLE["nonce"]),
        ("oauth_version", "1.0"),
        ("file", "vacation.jpg"),
        ("size", "original"),
    ]
    base = signature_base("GET", "http://photos.example.net/photos", params)
    assert hmac_sha1(base, EXAMPLE["secret"], EXAMPLE["token_secret"]) == EXAMPLE_SIGNATURE


def _with_api(trello: FakeTrello, use: Callable[[TrelloApi], Awaitable[Any]], **settings: Any) -> Any:
    async def run() -> Any:
        async with TestServer(trello.app) as server, TrelloApi(trello_settings(server, **settings)) as api:
            return await use(api)

    return asyncio.run(run())


def test_the_request_token_is_signed_and_asks_trello_to_call_the_bot_back() -> None:
    trello = FakeTrello()

    token, secret = _with_api(trello, lambda api: api.request_token())

    assert trello.request_tokens[token].secret == secret
    assert trello.request_tokens[token].callback == f"{PUBLIC_BASE_URL}/trello/callback"
    [call] = trello.requests
    assert (call.method, call.path, call.status) == ("POST", "/1/OAuthGetRequestToken", 200)


def test_the_authorize_link_asks_for_read_and_write_with_no_end_date() -> None:
    settings = TrelloSettings(
        api_key="k", api_secret="s", public_base_url=PUBLIC_BASE_URL, secret_key="x", auth_base="https://trello.example"
    )

    link = TrelloApi(settings).authorize_url("request-token-1")

    parts = urlsplit(link)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://trello.example/1/OAuthAuthorizeToken"
    assert query_of(link) == {
        "oauth_token": "request-token-1",
        "name": "todobot",
        "scope": "read,write",
        "expiration": "never",
    }
    assert "scope=read,write" in link  # the comma as it is: Trello's own links have it so


def test_the_access_token_comes_for_an_allowed_request_token_and_its_verifier() -> None:
    trello = FakeTrello()

    async def connect(api: TrelloApi) -> Access:
        token, secret = await api.request_token()
        verifier = query_of(trello.decide(token, allow=True))["oauth_verifier"]
        return await api.access_token(token, secret, verifier)

    access = _with_api(trello, connect)

    assert trello.access_tokens == {access.token: access.secret}


def test_a_wrong_verifier_gets_no_access_and_the_error_holds_no_secret() -> None:
    trello = FakeTrello()

    async def connect(api: TrelloApi) -> tuple[str, str, TrelloError]:
        token, secret = await api.request_token()
        trello.decide(token, allow=True)
        with pytest.raises(TrelloError) as raised:
            await api.access_token(token, secret, "not-the-verifier")
        return token, secret, raised.value

    token, secret, error = _with_api(trello, connect)

    assert error.status == 401
    assert str(error) == "POST /1/OAuthGetAccessToken: HTTP 401"
    assert token not in str(error)
    assert secret not in str(error)
    assert trello.access_tokens == {}


def test_rest_calls_are_signed_with_the_users_token_which_stays_out_of_urls() -> None:
    trello = FakeTrello()
    access = trello.issue_access()

    async def calls(api: TrelloApi) -> tuple[Any, Any]:
        boards = await api.get(access, "/1/members/me/boards", {"filter": "open", "fields": "name"})
        made = await api.post(access, "/1/boards", {"name": "todobot", "defaultLists": False})
        return boards, made

    boards, made = _with_api(trello, calls)

    assert boards == []
    assert made["name"] == "todobot"
    assert [(r.method, r.path, r.status) for r in trello.requests] == [
        ("GET", "/1/members/me/boards", 200),
        ("POST", "/1/boards", 200),
    ]
    get, post = trello.requests
    assert get.query == {"filter": "open", "fields": "name"}
    assert post.query == {}
    assert post.body == {"name": "todobot", "defaultLists": False}  # JSON, not in the URL
    for request in trello.requests:
        oauth = parse_authorization(request.authorization)
        assert oauth["oauth_token"] == access.token
        assert access.token not in request.path
        assert all(access.token not in value for value in request.query.values())


def test_a_refusal_is_a_trello_error_that_names_the_call_only() -> None:
    trello = FakeTrello(refusing=True)
    access = trello.issue_access()
    text = "Купить молоко"

    async def make(api: TrelloApi) -> TrelloError:
        with pytest.raises(TrelloError) as raised:
            await api.post(access, "/1/cards", {"idList": "x", "name": text})
        return raised.value

    error = _with_api(trello, make)

    assert (error.status, str(error)) == (500, "POST /1/cards: HTTP 500")
    assert text not in repr(error.args)


def test_an_unreachable_trello_is_a_trello_error_with_the_type_only() -> None:
    async def run() -> TrelloError:
        async with TestServer(FakeTrello().app) as server:
            settings = trello_settings(server)
        # the server is gone now: nothing listens at its address
        async with TrelloApi(settings) as api:
            with pytest.raises(TrelloError) as raised:
                await api.request_token()
            return raised.value

    error = asyncio.run(run())

    assert error.status is None
    assert error.error == "ClientConnectorError"
    assert str(error) == "POST /1/OAuthGetRequestToken: ClientConnectorError"


def test_access_is_kept_as_json_and_shows_no_secret() -> None:
    access = Access(token="tok-123", secret="sec-456")

    assert Access.loads(access.dumps()) == access
    assert "tok-123" not in repr(access)
    assert "sec-456" not in repr(access)


@pytest.mark.parametrize(
    "raw", ["", "not json", "[]", '{"token": ""}', '{"token": "t"}', '{"token": 1, "secret": "s"}']
)
def test_access_that_is_not_one_is_none(raw: str) -> None:
    assert Access.loads(raw) is None
