"""A fake Trello for the unit tests, in-process (aiohttp), serving both trello.com's OAuth 1.0a and the REST calls
the bot makes to api.trello.com. Every request's OAuth signature is checked with an RFC 5849 implementation of this
file's own, not oauthlib's (which the bot signs with), and a bad one is answered 401, as Trello does. Ids are shaped
like Trello's: the Unix time of making in 8 hex digits, then 16 more.

It can refuse every REST call (500), make a card yet answer 500 (an answer that never came back), and make a card at
once but answer only after a while (a slow answer).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from aiohttp import web

from todobot.trello.api import Access

KEY = "unit-trello-key-3f9c2a7e5b1d4c8a"
SECRET = "unit-trello-secret-8d1e6a0c4f2b9e7a"
LIST_GAP = 16384.0

_seq = itertools.count(1)


def trello_id(at: float | None = None) -> str:
    """An id as Trello makes them: the time first, so ids sort by when they were made."""
    return f"{int(time.time() if at is None else at):08x}{next(_seq):016x}"


# --- RFC 5849, written out here so that the bot's signatures (oauthlib's) are checked by something else ---------------


def _enc(value: str) -> str:
    """RFC 5849 3.6: percent-encode all but the unreserved characters (ALPHA, DIGIT, -, ., _, ~)."""
    return quote(value, safe="~")


def signature_base(method: str, base_uri: str, params: list[tuple[str, str]]) -> str:
    """RFC 5849 3.4.1: the method, the base string URI and the normalized parameters."""
    normalized = "&".join(f"{name}={value}" for name, value in sorted((_enc(n), _enc(v)) for n, v in params))
    return "&".join([method.upper(), _enc(base_uri), _enc(normalized)])


def hmac_sha1(base: str, consumer_secret: str, token_secret: str | None) -> str:
    """RFC 5849 3.4.2."""
    key = f"{_enc(consumer_secret)}&{_enc(token_secret or '')}"
    return base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()


def parse_authorization(header: str) -> dict[str, str]:
    """The parameters of `Authorization: OAuth k="v", …`, decoded."""
    if not header.startswith("OAuth "):
        return {}
    params = {}
    for part in header[len("OAuth ") :].split(","):
        name, _, value = part.strip().partition("=")
        params[unquote(name)] = unquote(value.strip('"'))
    return params


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, str]
    body: dict[str, Any] | None
    authorization: str
    status: int


@dataclass
class RequestToken:
    secret: str
    callback: str
    verifier: str | None = None


@dataclass
class FakeTrello:
    key: str = KEY
    secret: str = SECRET
    refusing: bool = False
    lose_card_answers: int = 0  # the next ones make the card and then answer 500
    card_answer_delay: float = 0.0  # seconds a card is on the board before its answer goes back
    request_tokens: dict[str, RequestToken] = field(default_factory=dict)
    access_tokens: dict[str, str] = field(default_factory=dict)  # token -> its secret
    boards: list[dict[str, Any]] = field(default_factory=list)
    lists: list[dict[str, Any]] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)
    requests: list[Recorded] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.app = web.Application()
        self.app.router.add_route("*", "/{tail:.*}", self.handle)

    # what tests set up and read

    def issue_access(self) -> Access:
        """An access token as if a user had connected."""
        access = Access(token=secrets.token_hex(32), secret=secrets.token_hex(16))
        self.access_tokens[access.token] = access.secret
        return access

    def decide(self, request_token: str, *, allow: bool) -> str:
        """The user allows or denies on the authorize page: the callback URL Trello sends their browser to."""
        token = self.request_tokens[request_token]
        back = {"oauth_token": request_token}
        if allow:
            token.verifier = secrets.token_hex(16)
            back["oauth_verifier"] = token.verifier
        separator = "&" if "?" in token.callback else "?"
        return f"{token.callback}{separator}{urlencode(back)}"

    def add_board(self, name: str, lists: list[str], *, closed: bool = False) -> str:
        board_id = trello_id()
        self.boards.append({"id": board_id, "name": name, "closed": closed})
        for number, list_name in enumerate(lists, 1):
            self.lists.append(
                {"id": trello_id(), "idBoard": board_id, "name": list_name, "pos": LIST_GAP * number, "closed": False}
            )
        return board_id

    def lists_of(self, board_id: str) -> list[dict[str, Any]]:
        return sorted((x for x in self.lists if x["idBoard"] == board_id and not x["closed"]), key=lambda x: x["pos"])

    def list_named(self, board_id: str, name: str) -> dict[str, Any]:
        return next(x for x in self.lists_of(board_id) if x["name"] == name)

    def board_named(self, name: str) -> dict[str, Any]:
        return next(b for b in self.boards if b["name"] == name and not b["closed"])

    def calls(self, method: str, path_start: str) -> list[Recorded]:
        return [r for r in self.requests if r.method == method and r.path.startswith(path_start)]

    # serving

    async def handle(self, request: web.Request) -> web.Response:
        raw = await request.read()
        body = json.loads(raw) if raw and request.content_type == "application/json" else None
        query = dict(request.rel_url.query)
        authorization = request.headers.get("Authorization", "")
        response = self._answer(request, query, authorization, body)
        self.requests.append(Recorded(request.method, request.path, query, body, authorization, response.status))
        if self.card_answer_delay and (request.method, request.path) == ("POST", "/1/cards"):
            await asyncio.sleep(self.card_answer_delay)
        return response

    def _answer(
        self, request: web.Request, query: dict[str, str], authorization: str, body: dict[str, Any] | None
    ) -> web.Response:
        oauth = parse_authorization(authorization)
        path = request.path
        if path == "/1/OAuthGetRequestToken":
            return self._request_token(request, query, oauth)
        if path == "/1/OAuthGetAccessToken":
            return self._access_token(request, query, oauth)
        if self.refusing:
            return web.Response(status=500, text="Internal Server Error")
        token = oauth.get("oauth_token", "")
        if token not in self.access_tokens:
            return web.Response(status=401, text="invalid token")
        if not self._signed(request, query, oauth, self.access_tokens[token]):
            return web.Response(status=401, text="invalid signature")
        if request.method == "POST":
            return self._post(path, body or {})
        return self._get(path, query)

    def _signed(
        self, request: web.Request, query: dict[str, str], oauth: dict[str, str], token_secret: str | None
    ) -> bool:
        if oauth.get("oauth_consumer_key") != self.key or oauth.get("oauth_signature_method") != "HMAC-SHA1":
            return False
        params = [(k, v) for k, v in oauth.items() if k not in ("oauth_signature", "realm")] + list(query.items())
        base_uri = f"{request.scheme}://{request.host}{request.rel_url.raw_path}"
        expected = hmac_sha1(signature_base(request.method, base_uri, params), self.secret, token_secret)
        return hmac.compare_digest(expected, oauth.get("oauth_signature", ""))

    def _request_token(self, request: web.Request, query: dict[str, str], oauth: dict[str, str]) -> web.Response:
        if request.method != "POST" or not self._signed(request, query, oauth, None):
            return web.Response(status=401, text="invalid signature")
        callback = oauth.get("oauth_callback", "")
        if not callback.startswith(("http://", "https://")):
            return web.Response(status=400, text="oauth_callback required")
        token, secret = secrets.token_hex(16), secrets.token_hex(16)
        self.request_tokens[token] = RequestToken(secret=secret, callback=callback)
        answer = {"oauth_token": token, "oauth_token_secret": secret, "oauth_callback_confirmed": "true"}
        return web.Response(text=urlencode(answer), content_type="application/x-www-form-urlencoded")

    def _access_token(self, request: web.Request, query: dict[str, str], oauth: dict[str, str]) -> web.Response:
        token = self.request_tokens.get(oauth.get("oauth_token", ""))
        if request.method != "POST" or token is None or not self._signed(request, query, oauth, token.secret):
            return web.Response(status=401, text="invalid signature")
        if token.verifier is None or oauth.get("oauth_verifier") != token.verifier:
            return web.Response(status=401, text="oauth_verifier is invalid")
        access = self.issue_access()
        del self.request_tokens[oauth["oauth_token"]]
        answer = {"oauth_token": access.token, "oauth_token_secret": access.secret}
        return web.Response(text=urlencode(answer), content_type="application/x-www-form-urlencoded")

    def _get(self, path: str, query: dict[str, str]) -> web.Response:
        parts = path.strip("/").split("/")  # ["1", ...]
        if parts[1:] == ["members", "me", "boards"]:
            wanted = query.get("filter", "all")
            boards = [b for b in self.boards if wanted != "open" or not b["closed"]]
            return web.json_response([{"id": b["id"], "name": b["name"]} for b in boards])
        if len(parts) >= 3 and parts[1] == "boards":
            board = next((b for b in self.boards if b["id"] == parts[2]), None)
            if board is None:
                return web.Response(status=404, text="The requested resource was not found.")
            if parts[3:] == ["lists"]:
                return web.json_response(self._lists_json(board["id"], query.get("filter", "open")))
            if parts[3:] == ["cards", "all"]:
                return web.json_response([self._card_json(c) for c in self.cards if c["idBoard"] == board["id"]])
            if parts[3:] == []:
                value: dict[str, Any] = {"id": board["id"], "name": board["name"]}
                if query.get("lists"):
                    value["lists"] = self._lists_json(board["id"], query["lists"])
                if query.get("cards") == "open":
                    value["cards"] = [
                        self._card_json(c) for c in self.cards if c["idBoard"] == board["id"] and not c["closed"]
                    ]
                return web.json_response(value)
        return web.Response(status=404, text=f"no route GET {path}")

    def _post(self, path: str, body: dict[str, Any]) -> web.Response:
        if path == "/1/boards":
            board_id = trello_id()
            self.boards.append({"id": board_id, "name": body["name"], "closed": False})
            return web.json_response({"id": board_id, "name": body["name"]})
        if path == "/1/lists":
            if not any(b["id"] == body.get("idBoard") for b in self.boards):
                return web.Response(status=400, text="invalid value for idBoard")
            pos = self._pos(body.get("pos"), [x["pos"] for x in self.lists if x["idBoard"] == body["idBoard"]])
            made = {"id": trello_id(), "idBoard": body["idBoard"], "name": body["name"], "pos": pos, "closed": False}
            self.lists.append(made)
            return web.json_response(made)
        if path == "/1/cards":
            trello_list = next((x for x in self.lists if x["id"] == body.get("idList")), None)
            if trello_list is None:
                return web.Response(status=400, text="invalid value for idList")
            card = {
                "id": trello_id(),
                "idBoard": trello_list["idBoard"],
                "idList": trello_list["id"],
                "name": body.get("name", ""),
                "desc": body.get("desc", ""),
                "closed": False,
            }
            self.cards.append(card)
            if self.lose_card_answers:
                self.lose_card_answers -= 1
                return web.Response(status=500, text="Internal Server Error")
            return web.json_response(self._card_json(card))
        return web.Response(status=404, text=f"no route POST {path}")

    @staticmethod
    def _pos(value: Any, taken: list[float]) -> float:
        if value in (None, "bottom"):
            return (max(taken) if taken else 0.0) + LIST_GAP
        return float(value)

    def _lists_json(self, board_id: str, wanted: str) -> list[dict[str, Any]]:
        lists = sorted((x for x in self.lists if x["idBoard"] == board_id), key=lambda x: x["pos"])
        return [
            {"id": x["id"], "name": x["name"], "pos": x["pos"]} for x in lists if wanted == "all" or not x["closed"]
        ]

    @staticmethod
    def _card_json(card: dict[str, Any]) -> dict[str, Any]:
        return {k: card[k] for k in ("id", "name", "desc", "idList", "idBoard", "closed")}


def callback_path(url: str) -> str:
    """The path and query of the callback URL, for the app's test client."""
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


def query_of(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


@contextmanager
def refusing(trello: FakeTrello) -> Iterator[None]:
    trello.refusing = True
    try:
        yield
    finally:
        trello.refusing = False
