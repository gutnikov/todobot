"""What the TRL acceptance tests need beyond SK's, DUP's, ERR's, ACC's and CAP's harness
(agentify/specs/requirements/TRL.yaml, `seams`).

- A fake Trello (FakeTrello): one local HTTP server, used as both TRELLO_AUTH_BASE and TRELLO_API_BASE, with a state of
  its own: one member, Анна, with boards, lists (name, position, open or archived) and cards (name, description, list,
  open or archived, the order they were made in). It serves
  - Trello's OAuth 1.0a: `POST|GET /1/OAuthGetRequestToken` (answers `oauth_token`, `oauth_token_secret`,
    form-encoded), the page `GET /1/OAuthAuthorizeToken` (it records the query: `oauth_token`, `scope`, `expiration`,
    `name`) and its form `POST /1/OAuthAuthorizeToken` (Allow or Deny), and `POST|GET /1/OAuthGetAccessToken`.
    Signatures are not checked; what each request carried (an `Authorization: OAuth …` header, oauth_* parameters)
    is recorded. Allowing sends the browser (303) to the bot's `oauth_callback` with `oauth_token` and
    `oauth_verifier`; denying sends it to the same callback with `oauth_token` only, without an `oauth_verifier`, and
    that request token can then not be exchanged for an access token.
  - The REST API under /1/: the member's boards, a board, its lists and cards, a list and its cards, a card, search
    and batch; creating boards, lists and cards; changing them. A call is authorised by an access token the fake
    issued, OAuth-signed (`oauth_consumer_key`, `oauth_token`) or as `key` and `token` parameters; otherwise 401, as
    Trello answers. A board it creates gets no lists of its own (real Trello adds three unless `defaultLists=false`:
    the request's parameters are recorded, and the board test checks that flag).
  - Trello refusing: every REST call answers 500 (OAuth keeps working) until the test makes it take again.
  Every request is recorded, so a failure shows what the bot asked Trello for.
- `serve` (after `migrate`) with TRL's environment, open to everyone and without PostHog like CAP's tests: the state
  runningBotWithTrello. PUBLIC_BASE_URL is the app's own address in the test.
- Анна's browser: an HTTP client that opens the link the bot sent, decides on Trello's page and follows the redirect
  to the bot's callback, never leaving 127.0.0.1.
- The states annaAskedForTrello and annaConnectedTrello, reached by driving the bot; what Анна changes in Trello (a card
  moved, renamed, archived) is changed in the fake, as Trello's own state.

Black-box like harness.py: nothing here imports the app.
"""

from __future__ import annotations

import base64
import copy
import html
import itertools
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

import httpx
from cap_harness import NOT_INHERITED as CAP_NOT_INHERITED
from dup_harness import ANNA, GIVE_UP_AFTER, QUIET_PERIOD
from harness import (
    SAVED_REPLY,
    ApiCall,
    AppProcess,
    Delivery,
    FakeBotApi,
    _parse_params,
    app_env,
    command_update,
    deliver,
    free_port,
    run_migrate,
    text_update,
)

# --- The bot's texts, byte for byte from TRL#1 and TRL#4 --------------------------------

LINK_PREFIX = "Чтобы подключить Trello, открой ссылку и разреши доступ: "  # followed by the link
CONNECTED_PAGE = "Trello подключён, можно возвращаться в Telegram."
CONNECTED_REPLY = "Trello подключён ✅ Задачи будут на доске «todobot»."
DECLINED_PAGE = "Trello не подключён."
DECLINED_REPLY = "Trello не подключён: доступ не разрешён."
TRELLO_DOWN_REPLY = "Trello сейчас не отвечает, попробуй позже."
NOT_IN_TRELLO_LABEL = "ещё не в Trello"

BOARD_NAME = "todobot"
# The rule status-list: the board's lists, in this order; a card's label in /tasks is its list's name in lower case.
STATUS_LISTS = ("Записана", "Исследуется", "Ждёт ответов", "План готов", "Сделана")
RECORDED_LIST = STATUS_LISTS[0]

# --- What the tests configure the app with (all fake) ------------------------------------

TRELLO_API_KEY = "3f9c2a7e5b1d4c8a9e0f6b2d7c4a1e85"
TRELLO_API_SECRET = "8d1e6a0c4f2b9e7a5c3d1f0b8e6a4c2d9f7b5e3a1c0d8f6b4a2e0c9d7f5b3a1e"
# Any test value; this one is also a valid Fernet key (32 bytes, url-safe base64), should the app want one.
APP_SECRET_KEY = base64.urlsafe_b64encode(b"todobot-acceptance-secret-key-32").decode("ascii")
TRELLO_RETRY_SECONDS = 1  # TRL#3's "once a minute", made short for the tests
CALLBACK_PATH = "/trello/callback"  # the seam: GET {PUBLIC_BASE_URL}/trello/callback

TRELLO_ENV_VARS = (
    "TRELLO_API_KEY",
    "TRELLO_API_SECRET",
    "PUBLIC_BASE_URL",
    "TRELLO_API_BASE",
    "TRELLO_AUTH_BASE",
    "TRELLO_RETRY_SECONDS",
    "APP_SECRET_KEY",
)

POLL_INTERVAL = 0.05
BROWSER_TIMEOUT = 15.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
LIST_GAP = 16384.0  # Trello's step between positions

MEMBER = {"id": "5e8f1c2b3a4d5e6f7a8b9c0d", "username": "anna_tasks", "fullName": "Анна"}

_ids = itertools.count(1)


def _new_id() -> str:
    """An id shaped like Trello's: 24 hex digits."""
    return f"{0x665F0A1B2C3D4E5F00000000 + next(_ids):024x}"


def trello_flag(value: Any) -> bool | None:
    """A Trello boolean parameter: true/false as JSON or text; None when absent or something else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


# --- Trello's state --------------------------------------------------------------------


@dataclass
class Board:
    id: str
    name: str
    closed: bool = False
    desc: str = ""


@dataclass
class TrelloList:
    id: str
    id_board: str
    name: str
    pos: float
    closed: bool = False


@dataclass
class Card:
    id: str
    id_board: str
    id_list: str
    name: str
    desc: str
    pos: float
    seq: int  # the order the cards were made in
    closed: bool = False


@dataclass
class RequestToken:
    token: str
    secret: str
    callback: str | None  # the bot's oauth_callback; None for "oob" or none given
    verifier: str | None = None
    decision: str | None = None  # "allow" or "deny", once Анна decided on the page


@dataclass
class AuthorizeVisit:
    """One time a browser opened Trello's authorize page."""

    query: dict[str, str]
    known_token: bool  # the oauth_token is a request token the fake issued

    @property
    def oauth_token(self) -> str | None:
        return self.query.get("oauth_token")


@dataclass
class TrelloRequest:
    """One request the app (or the browser) made to the fake Trello."""

    http_method: str
    path: str
    params: dict[str, Any]  # query and body
    oauth: dict[str, str]  # oauth_* parameters, from the Authorization header, the query or the body
    oauth_header: bool  # carried `Authorization: OAuth …`
    kind: str  # "oauth" (token endpoints), "authorize" (the page and its form), "rest", "other"
    status: int
    note: str = ""

    @property
    def signed(self) -> bool:
        return self.oauth_header or bool(self.oauth)

    def short(self) -> str:
        if self.oauth_header:
            via = "OAuth header"
        elif self.oauth:
            via = "oauth_* params"
        elif "token" in self.params:
            via = "key/token"
        else:
            via = "no credentials"
        shown = {k: v for k, v in self.params.items() if not k.startswith("oauth_") and k not in ("key", "token")}
        text = json.dumps(shown, ensure_ascii=False)
        if len(text) > 300:
            text = text[:300] + "…"
        note = f" ({self.note})" if self.note else ""
        return f"{self.http_method} {self.path} -> {self.status}{note} [{via}] {text}"


@dataclass
class _Answer:
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    data: Any = None  # the JSON value, for batch
    note: str = ""


class _TrelloError(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text


def _json(value: Any, status: int = 200) -> _Answer:
    return _Answer(
        status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False).encode(), data=value
    )


def _text(status: int, text: str, note: str = "") -> _Answer:
    return _Answer(status, "text/plain; charset=utf-8", text.encode(), note=note or text)


def _form(values: dict[str, str]) -> _Answer:
    return _Answer(200, "application/x-www-form-urlencoded", urlencode(values).encode())


def _page(status: int, body: str) -> _Answer:
    return _Answer(status, "text/html; charset=utf-8", body.encode())


_OAUTH_HEADER_PARAM = re.compile(r'([A-Za-z0-9_]+)\s*=\s*"([^"]*)"')


def _oauth_params(header: str, params: dict[str, Any]) -> tuple[dict[str, str], bool]:
    """The oauth_* parameters of a request and whether it carried `Authorization: OAuth …`."""
    oauth = {k: str(v) for k, v in params.items() if k.startswith("oauth_")}
    has_header = header[:6].lower() == "oauth "
    if has_header:
        for name, value in _OAUTH_HEADER_PARAM.findall(header[6:]):
            if name.startswith("oauth_"):
                oauth[name] = unquote(value)
    return oauth, has_header


class FakeTrello:
    """A local stand-in for trello.com (OAuth 1.0a) and api.trello.com (REST), with one member, Анна."""

    def __init__(self, api_key: str = TRELLO_API_KEY) -> None:
        self.api_key = api_key
        self._lock = threading.RLock()
        self._requests: list[TrelloRequest] = []
        self._visits: list[AuthorizeVisit] = []
        self._request_tokens: dict[str, RequestToken] = {}
        self._access_tokens: dict[str, str] = {}  # access token -> its secret
        self._boards: list[Board] = []
        self._lists: list[TrelloList] = []
        self._cards: list[Card] = []
        self._card_seq = itertools.count(1)
        self._refusing = False
        self._routes = self._route_table()
        trello = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                trello._handle(self)

            def do_POST(self) -> None:
                trello._handle(self)

            def do_PUT(self) -> None:
                trello._handle(self)

            def do_DELETE(self) -> None:
                trello._handle(self)

            def do_PATCH(self) -> None:
                trello._handle(self)

            def log_message(self, *args: Any) -> None:  # silence stderr
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-trello", daemon=True)

    # lifecycle

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeTrello:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # outcomes: Trello takes, refuses, recovers

    def refuse(self) -> None:
        """Every REST call answers 500 from now on (OAuth keeps working)."""
        with self._lock:
            self._refusing = True

    def take(self) -> None:
        """Trello answers REST calls again."""
        with self._lock:
            self._refusing = False

    # request handling

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        body = FakeBotApi._read_body(handler)  # chunked or Content-Length, as SK's fake Bot API reads it
        split = urlsplit(handler.path)
        method = handler.command.upper()
        path = split.path.rstrip("/") or "/"
        try:
            params = _parse_params(handler.headers.get("Content-Type", ""), body, split.query)
        except (ValueError, UnicodeDecodeError) as exc:
            params, problem = {}, _text(400, f"unparsable body: {exc}")
        else:
            problem = None
        oauth, oauth_header = _oauth_params(handler.headers.get("Authorization", ""), params)
        with self._lock:
            kind, answer = ("other", problem) if problem else self._dispatch(method, path, params, oauth)
            self._requests.append(
                TrelloRequest(method, path, params, oauth, oauth_header, kind, answer.status, answer.note)
            )
        handler.send_response(answer.status)
        handler.send_header("Content-Type", answer.content_type)
        handler.send_header("Content-Length", str(len(answer.body)))
        for name, value in answer.headers.items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(answer.body)

    def _dispatch(self, method: str, path: str, params: dict[str, Any], oauth: dict[str, str]) -> tuple[str, _Answer]:
        if path == "/1/OAuthGetRequestToken":
            return "oauth", self._request_token(oauth)
        if path == "/1/OAuthGetAccessToken":
            return "oauth", self._access_token(oauth)
        if path == "/1/OAuthAuthorizeToken":
            if method == "GET":
                return "authorize", self._authorize_page(params)
            return "authorize", self._authorize_decision(params)
        if not path.startswith("/1/"):
            return "other", _text(404, "The requested resource was not found.", note="not a Trello path")
        if self._refusing:
            return "rest", _text(500, "Internal Server Error", note="Trello refuses")
        key = oauth.get("oauth_consumer_key") or params.get("key")
        token = oauth.get("oauth_token") or params.get("token")
        if key != self.api_key:
            return "rest", _text(401, "invalid key")
        if token not in self._access_tokens:
            return "rest", _text(401, "invalid token")
        return "rest", self._rest(method, path.removeprefix("/1/"), params)

    # OAuth 1.0a

    def _request_token(self, oauth: dict[str, str]) -> _Answer:
        if oauth.get("oauth_consumer_key") != self.api_key:
            return _text(401, "invalid key", note=f"oauth_consumer_key={oauth.get('oauth_consumer_key')!r}")
        token, secret = secrets.token_hex(16), secrets.token_hex(16)
        callback = oauth.get("oauth_callback")
        self._request_tokens[token] = RequestToken(token, secret, None if callback in (None, "", "oob") else callback)
        return _form({"oauth_token": token, "oauth_token_secret": secret, "oauth_callback_confirmed": "true"})

    def _authorize_page(self, params: dict[str, Any]) -> _Answer:
        query = {k: str(v) for k, v in params.items()}
        token = query.get("oauth_token", "")
        request_token = self._request_tokens.get(token)
        self._visits.append(AuthorizeVisit(query, request_token is not None))
        if request_token is None:
            return _page(400, "<!doctype html><p>Invalid token.</p>")
        if request_token.callback is None:  # tolerated: a callback named on the authorize page instead
            request_token.callback = query.get("oauth_callback") or query.get("return_url") or None
        name = html.escape(query.get("name", "An application"))
        return _page(
            200,
            "<!doctype html><title>Trello</title>"
            f"<p>{name} asks for access to your Trello account (scope {html.escape(query.get('scope', ''))}, "
            f"expiration {html.escape(query.get('expiration', ''))}).</p>"
            '<form method="post" action="/1/OAuthAuthorizeToken">'
            f'<input type="hidden" name="oauth_token" value="{html.escape(token)}">'
            '<button name="approve" value="Allow">Allow</button> <button name="deny" value="Deny">Deny</button>'
            "</form>",
        )

    def _authorize_decision(self, params: dict[str, Any]) -> _Answer:
        token = str(params.get("oauth_token", ""))
        request_token = self._request_tokens.get(token)
        if request_token is None:
            return _page(400, "<!doctype html><p>Invalid token.</p>")
        allow = "approve" in params
        request_token.decision = "allow" if allow else "deny"
        back = {"oauth_token": token}
        if allow:
            request_token.verifier = secrets.token_hex(16)
            back["oauth_verifier"] = request_token.verifier
        if request_token.callback is None:
            shown = f"Your verification code is {request_token.verifier}" if allow else "Access denied."
            return _page(200, f"<!doctype html><p>{shown}</p>")
        separator = "&" if "?" in request_token.callback else "?"
        return _Answer(303, "text/plain", b"", {"Location": request_token.callback + separator + urlencode(back)})

    def _access_token(self, oauth: dict[str, str]) -> _Answer:
        if oauth.get("oauth_consumer_key") != self.api_key:
            return _text(401, "invalid key", note=f"oauth_consumer_key={oauth.get('oauth_consumer_key')!r}")
        request_token = self._request_tokens.get(oauth.get("oauth_token", ""))
        if request_token is None:
            return _text(401, "invalid token")
        if request_token.decision != "allow" or oauth.get("oauth_verifier") != request_token.verifier:
            return _text(401, "oauth_verifier is invalid", note=f"the token's decision: {request_token.decision}")
        access, secret = secrets.token_hex(32), secrets.token_hex(16)
        self._access_tokens[access] = secret
        return _form({"oauth_token": access, "oauth_token_secret": secret})

    # REST

    def _route_table(self) -> list[tuple[str, re.Pattern[str], Callable[..., _Answer]]]:
        routes: list[tuple[str, str, Callable[..., _Answer]]] = [
            ("GET", r"members/(?P<member>[^/]+)/boards(?:/(?P<filter_name>\w+))?", self._member_boards),
            ("GET", r"members/(?P<member>[^/]+)", self._member),
            ("POST", r"boards", self._create_board),
            ("GET", r"boards/(?P<board_id>[^/]+)", self._get_board),
            ("PUT", r"boards/(?P<board_id>[^/]+)", self._update_board),
            ("GET", r"boards/(?P<board_id>[^/]+)/lists(?:/(?P<filter_name>\w+))?", self._board_lists),
            ("POST", r"boards/(?P<board_id>[^/]+)/lists", self._create_list_on_board),
            ("GET", r"boards/(?P<board_id>[^/]+)/cards(?:/(?P<filter_name>\w+))?", self._board_cards),
            ("POST", r"lists", self._create_list),
            ("GET", r"lists/(?P<list_id>[^/]+)", self._get_list),
            ("PUT", r"lists/(?P<list_id>[^/]+)", self._update_list),
            ("PUT", r"lists/(?P<list_id>[^/]+)/closed", self._close_list),
            ("GET", r"lists/(?P<list_id>[^/]+)/cards(?:/(?P<filter_name>\w+))?", self._list_cards),
            ("POST", r"lists/(?P<list_id>[^/]+)/cards", self._create_card_in_list),
            ("POST", r"cards", self._create_card),
            ("GET", r"cards/(?P<card_id>[^/]+)", self._get_card),
            ("GET", r"cards/(?P<card_id>[^/]+)/list", self._card_list),
            ("GET", r"cards/(?P<card_id>[^/]+)/(?P<attr>name|desc|closed|idList|idBoard)", self._card_field),
            ("PUT", r"cards/(?P<card_id>[^/]+)", self._update_card),
            ("DELETE", r"cards/(?P<card_id>[^/]+)", self._delete_card),
            ("GET", r"search", self._search),
            ("GET", r"batch", self._batch),
        ]
        return [(method, re.compile(pattern), handler) for method, pattern, handler in routes]

    def _rest(self, method: str, rest_path: str, params: dict[str, Any]) -> _Answer:
        for route_method, pattern, handler in self._routes:
            match = pattern.fullmatch(rest_path) if route_method == method else None
            if match:
                try:
                    return handler(params, **match.groupdict())
                except _TrelloError as problem:
                    return _text(problem.status, problem.text)
        return _text(404, f"Cannot {method} /1/{rest_path}", note="no such route in the fake")

    # lookups

    def _board(self, board_id: str) -> Board:
        for board in self._boards:
            if board.id == board_id:
                return board
        raise _TrelloError(404, "The requested resource was not found.")

    def _list(self, list_id: str) -> TrelloList:
        for trello_list in self._lists:
            if trello_list.id == list_id:
                return trello_list
        raise _TrelloError(404, "The requested resource was not found.")

    def _card(self, card_id: str) -> Card:
        for card in self._cards:
            if card.id == card_id:
                return card
        raise _TrelloError(404, "The requested resource was not found.")

    @staticmethod
    def _pos(value: Any, taken: list[float]) -> float:
        if value in (None, "", "bottom"):
            return (max(taken) if taken else 0.0) + LIST_GAP
        if value == "top":
            return min(taken) / 2 if taken else LIST_GAP
        try:
            return float(value)
        except (TypeError, ValueError):
            raise _TrelloError(400, "invalid value for pos") from None

    # JSON as Trello shapes it

    @staticmethod
    def _wanted(filter_value: Any, default: str) -> str:
        value = str(filter_value or default).lower()
        return value if value in ("open", "closed", "all", "none", "visible") else default

    def _lists_of(self, board_id: str, wanted: str) -> list[TrelloList]:
        lists = sorted((x for x in self._lists if x.id_board == board_id), key=lambda x: x.pos)
        return [x for x in lists if wanted == "all" or (wanted == "closed") == x.closed] if wanted != "none" else []

    def _cards_of(self, *, board_id: str | None = None, list_id: str | None = None, wanted: str) -> list[Card]:
        cards = [
            c
            for c in self._cards
            if (board_id is None or c.id_board == board_id) and (list_id is None or c.id_list == list_id)
        ]
        cards.sort(key=lambda c: (self._list(c.id_list).pos, c.pos))
        if wanted == "none":
            return []
        if wanted == "all":
            return cards
        if wanted == "closed":
            return [c for c in cards if c.closed]
        if wanted == "visible":
            return [c for c in cards if not c.closed and not self._list(c.id_list).closed]
        return [c for c in cards if not c.closed]

    def _card_json(self, card: Card) -> dict[str, Any]:
        return {
            "id": card.id,
            "name": card.name,
            "desc": card.desc,
            "closed": card.closed,
            "idList": card.id_list,
            "idBoard": card.id_board,
            "pos": card.pos,
            "idShort": card.seq,
            "shortLink": card.id[-8:],
            "url": f"https://trello.com/c/{card.id[-8:]}",
            "idMembers": [],
            "labels": [],
            "dateLastActivity": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }

    def _list_json(self, trello_list: TrelloList, params: dict[str, Any]) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": trello_list.id,
            "name": trello_list.name,
            "closed": trello_list.closed,
            "idBoard": trello_list.id_board,
            "pos": trello_list.pos,
            "subscribed": False,
        }
        if params.get("cards"):
            wanted = self._wanted(params.get("cards"), "none")
            value["cards"] = [self._card_json(c) for c in self._cards_of(list_id=trello_list.id, wanted=wanted)]
        return value

    def _board_json(self, board: Board, params: dict[str, Any]) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": board.id,
            "name": board.name,
            "desc": board.desc,
            "closed": board.closed,
            "idOrganization": None,
            "url": f"https://trello.com/b/{board.id[-8:]}/{board.name}",
            "shortUrl": f"https://trello.com/b/{board.id[-8:]}",
        }
        if params.get("lists"):
            wanted = self._wanted(params.get("lists"), "none")
            value["lists"] = [self._list_json(x, {}) for x in self._lists_of(board.id, wanted)]
        if params.get("cards"):
            wanted = self._wanted(params.get("cards"), "none")
            value["cards"] = [self._card_json(c) for c in self._cards_of(board_id=board.id, wanted=wanted)]
        return value

    # routes (the keyword arguments are the route's named groups)

    def _member(self, params: dict[str, Any], member: str) -> _Answer:
        value: dict[str, Any] = {**MEMBER, "idBoards": [b.id for b in self._boards]}
        if params.get("boards"):
            value["boards"] = self._member_boards({"filter": params.get("boards")}, member).data
        return _json(value)

    def _member_boards(self, params: dict[str, Any], member: str, filter_name: str | None = None) -> _Answer:
        wanted = self._wanted(filter_name or params.get("filter"), "all")
        boards = [b for b in self._boards if wanted in ("all", "visible") or (wanted == "closed") == b.closed]
        return _json([self._board_json(b, params) for b in boards])

    def _create_board(self, params: dict[str, Any]) -> _Answer:
        name = str(params.get("name") or "")
        if not name:
            raise _TrelloError(400, "invalid value for name")
        board = Board(_new_id(), name, desc=str(params.get("desc") or ""))
        self._boards.append(board)  # no lists of its own, whatever defaultLists says (it is recorded)
        return _json(self._board_json(board, {}))

    def _get_board(self, params: dict[str, Any], board_id: str) -> _Answer:
        return _json(self._board_json(self._board(board_id), params))

    def _update_board(self, params: dict[str, Any], board_id: str) -> _Answer:
        found = self._board(board_id)
        if "name" in params:
            found.name = str(params["name"])
        if trello_flag(params.get("closed")) is not None:
            found.closed = bool(trello_flag(params.get("closed")))
        if "desc" in params:
            found.desc = str(params["desc"])
        return _json(self._board_json(found, {}))

    def _board_lists(self, params: dict[str, Any], board_id: str, filter_name: str | None = None) -> _Answer:
        self._board(board_id)
        wanted = self._wanted(filter_name or params.get("filter"), "open")
        return _json([self._list_json(x, params) for x in self._lists_of(board_id, wanted)])

    def _add_list(self, board_id: str, params: dict[str, Any]) -> _Answer:
        self._board(board_id)
        name = str(params.get("name") or "")
        if not name:
            raise _TrelloError(400, "invalid value for name")
        taken = [x.pos for x in self._lists if x.id_board == board_id]
        trello_list = TrelloList(_new_id(), board_id, name, self._pos(params.get("pos"), taken))
        self._lists.append(trello_list)
        return _json(self._list_json(trello_list, {}))

    def _create_list_on_board(self, params: dict[str, Any], board_id: str) -> _Answer:
        return self._add_list(board_id, params)

    def _create_list(self, params: dict[str, Any]) -> _Answer:
        board_id = str(params.get("idBoard") or "")
        if not board_id:
            raise _TrelloError(400, "invalid value for idBoard")
        return self._add_list(board_id, params)

    def _board_cards(self, params: dict[str, Any], board_id: str, filter_name: str | None = None) -> _Answer:
        self._board(board_id)
        wanted = self._wanted(filter_name or params.get("filter"), "visible")
        return _json([self._card_json(c) for c in self._cards_of(board_id=board_id, wanted=wanted)])

    def _get_list(self, params: dict[str, Any], list_id: str) -> _Answer:
        return _json(self._list_json(self._list(list_id), params))

    def _update_list(self, params: dict[str, Any], list_id: str) -> _Answer:
        found = self._list(list_id)
        if "name" in params:
            found.name = str(params["name"])
        if trello_flag(params.get("closed")) is not None:
            found.closed = bool(trello_flag(params.get("closed")))
        if "pos" in params:
            found.pos = self._pos(params["pos"], [x.pos for x in self._lists if x.id_board == found.id_board])
        return _json(self._list_json(found, {}))

    def _close_list(self, params: dict[str, Any], list_id: str) -> _Answer:
        found = self._list(list_id)
        found.closed = bool(trello_flag(params.get("value")))
        return _json(self._list_json(found, {}))

    def _list_cards(self, params: dict[str, Any], list_id: str, filter_name: str | None = None) -> _Answer:
        self._list(list_id)
        wanted = self._wanted(filter_name or params.get("filter"), "open")
        return _json([self._card_json(c) for c in self._cards_of(list_id=list_id, wanted=wanted)])

    def _add_card(self, list_id: str, params: dict[str, Any]) -> _Answer:
        if not list_id:
            raise _TrelloError(400, "invalid value for idList")
        trello_list = self._list(list_id)
        taken = [c.pos for c in self._cards if c.id_list == list_id]
        card = Card(
            _new_id(),
            trello_list.id_board,
            list_id,
            str(params.get("name") or ""),
            str(params.get("desc") or ""),
            self._pos(params.get("pos"), taken),
            next(self._card_seq),
        )
        self._cards.append(card)
        return _json(self._card_json(card))

    def _create_card(self, params: dict[str, Any]) -> _Answer:
        return self._add_card(str(params.get("idList") or ""), params)

    def _create_card_in_list(self, params: dict[str, Any], list_id: str) -> _Answer:
        return self._add_card(list_id, params)

    def _get_card(self, params: dict[str, Any], card_id: str) -> _Answer:
        return _json(self._card_json(self._card(card_id)))

    def _card_list(self, params: dict[str, Any], card_id: str) -> _Answer:
        return _json(self._list_json(self._list(self._card(card_id).id_list), {}))

    def _card_field(self, params: dict[str, Any], card_id: str, attr: str) -> _Answer:
        return _json({"_value": self._card_json(self._card(card_id))[attr]})

    def _update_card(self, params: dict[str, Any], card_id: str) -> _Answer:
        found = self._card(card_id)
        if "name" in params:
            found.name = str(params["name"])
        if "desc" in params:
            found.desc = str(params["desc"])
        if params.get("idList"):
            found.id_list = self._list(str(params["idList"])).id
        if trello_flag(params.get("closed")) is not None:
            found.closed = bool(trello_flag(params.get("closed")))
        return _json(self._card_json(found))

    def _delete_card(self, params: dict[str, Any], card_id: str) -> _Answer:
        self._cards.remove(self._card(card_id))
        return _json({"limits": {}})

    def _search(self, params: dict[str, Any]) -> _Answer:
        query = str(params.get("query") or "").casefold()
        types = str(params.get("modelTypes") or "all").split(",")
        result: dict[str, Any] = {"options": {"terms": [{"text": query}]}}
        if "all" in types or "boards" in types:
            result["boards"] = [self._board_json(b, {}) for b in self._boards if query in b.name.casefold()]
        if "all" in types or "cards" in types:
            result["cards"] = [self._card_json(c) for c in self._cards if query in c.name.casefold() and not c.closed]
        return _json(result)

    def _batch(self, params: dict[str, Any]) -> _Answer:
        results: list[Any] = []
        for url in str(params.get("urls") or "").split(","):
            split = urlsplit(url.strip())
            path = split.path.strip("/").removeprefix("1/")
            inner = {k: v[-1] for k, v in parse_qs(split.query, keep_blank_values=True).items()}
            answer = self._rest("GET", path, inner)
            if answer.status == 200:
                results.append({"200": answer.data})
            else:
                results.append({"statusCode": answer.status, "message": answer.body.decode("utf-8", "replace")})
        return _json(results)

    # what the tests read and change: Анна's Trello

    def add_board(self, name: str, lists: list[str]) -> Board:
        """A board Анна made herself in Trello, with these lists in this order."""
        with self._lock:
            board = Board(_new_id(), name)
            self._boards.append(board)
            for number, list_name in enumerate(lists, 1):
                self._lists.append(TrelloList(_new_id(), board.id, list_name, LIST_GAP * number))
            return copy.copy(board)

    @property
    def boards(self) -> list[Board]:
        """Every board of Анна's, archived ones too, as copies."""
        with self._lock:
            return [copy.copy(b) for b in self._boards]

    def boards_named(self, name: str, *, include_closed: bool = False) -> list[Board]:
        return [b for b in self.boards if b.name == name and (include_closed or not b.closed)]

    def lists_of(self, board_id: str, *, include_closed: bool = False) -> list[TrelloList]:
        """The board's lists by position, as copies."""
        with self._lock:
            return [copy.copy(x) for x in self._lists_of(board_id, "all" if include_closed else "open")]

    def list_name(self, list_id: str) -> str | None:
        with self._lock:
            return next((x.name for x in self._lists if x.id == list_id), None)

    def cards_on(self, board_name: str = BOARD_NAME, *, include_closed: bool = False) -> list[Card]:
        """The cards on the boards called `board_name`, in the order they were made, as copies."""
        with self._lock:
            board_ids = {b.id for b in self._boards if b.name == board_name}
            cards = sorted(self._cards, key=lambda c: c.seq)
            return [copy.copy(c) for c in cards if c.id_board in board_ids and (include_closed or not c.closed)]

    def move_card(self, card_id: str, list_name: str) -> None:
        """Анна drags the card to the list `list_name` of its board."""
        with self._lock:
            card = self._card(card_id)
            card.id_list = next(x.id for x in self._lists if x.id_board == card.id_board and x.name == list_name)

    def rename_card(self, card_id: str, name: str) -> None:
        with self._lock:
            self._card(card_id).name = name

    def archive_card(self, card_id: str) -> None:
        with self._lock:
            self._card(card_id).closed = True

    # what Trello was asked

    def mark(self) -> int:
        """A position in the request log; pass it as `since` to look only at later requests."""
        with self._lock:
            return len(self._requests)

    def requests(self, since: int = 0, *, kind: str | None = None) -> list[TrelloRequest]:
        with self._lock:
            return [r for r in self._requests[since:] if kind is None or r.kind == kind]

    def visit_mark(self) -> int:
        with self._lock:
            return len(self._visits)

    def visits(self, since: int = 0) -> list[AuthorizeVisit]:
        """The times a browser opened the authorize page, with the query it carried."""
        with self._lock:
            return list(self._visits[since:])

    @property
    def access_tokens_issued(self) -> int:
        with self._lock:
            return len(self._access_tokens)

    def board_creations(self) -> list[TrelloRequest]:
        """The board-creating requests Trello took."""
        return [
            r
            for r in self.requests(kind="rest")
            if r.http_method == "POST" and r.path == "/1/boards" and r.status == 200
        ]

    def describe_requests(self) -> str:
        requests = self.requests()
        if not requests:
            return "  (Trello got no requests)"
        return "\n".join(f"  {r.short()}" for r in requests)

    def describe_state(self) -> str:
        with self._lock:
            lines = [] if self._boards else ["  (no boards)"]
            for board in self._boards:
                lines.append(f"  board «{board.name}» id={board.id}{' (archived)' if board.closed else ''}")
                for trello_list in self._lists_of(board.id, "all"):
                    archived = " (archived)" if trello_list.closed else ""
                    lines.append(f"    list «{trello_list.name}» pos={trello_list.pos:g}{archived}")
                    for card in sorted((c for c in self._cards if c.id_list == trello_list.id), key=lambda c: c.seq):
                        archived = " (archived)" if card.closed else ""
                        lines.append(f"      card #{card.seq} «{card.name}» desc={card.desc!r}{archived}")
            tokens = (
                f"  OAuth: {len(self._request_tokens)} request token(s), decisions "
                f"{[t.decision for t in self._request_tokens.values()]}, {len(self._access_tokens)} access token(s)"
            )
            return "\n".join([*lines, tokens])


# --- Reading what Анна sees ----------------------------------------------------------------


def visible_text(call: ApiCall) -> str:
    """The text of a sendMessage as Telegram shows it: with parse_mode HTML, its tags dropped and entities unescaped."""
    text = str(call.params.get("text", ""))
    if str(call.params.get("parse_mode", "")).lower() == "html":
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return text


def page_text(body: str) -> str:
    """What a browser shows of a page: its text without tags, scripts and styles, entities unescaped, white space
    collapsed (a plain-text page is shown as it is)."""
    body = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", body)
    body = re.sub(r"(?s)<[^>]*>", " ", body)
    return " ".join(html.unescape(body).split())


def wait_for[T](probe: Callable[[], T], *, within: float = GIVE_UP_AFTER) -> T:
    """`probe()` as soon as it is truthy, or its last value after `within` s."""
    deadline = time.monotonic() + within
    while True:
        value = probe()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(POLL_INTERVAL)


def settle(seconds: float) -> None:
    """A bounded quiet period: that nothing more happens (no second board, no second card) cannot be polled for."""
    time.sleep(seconds)


# --- The app with Trello -------------------------------------------------------------------


def trello_env(trello_base: str, port: int) -> dict[str, str]:
    """TRL's environment: the bot's Trello app, its own address, Trello at the fake, short retries."""
    return {
        "TRELLO_API_KEY": TRELLO_API_KEY,
        "TRELLO_API_SECRET": TRELLO_API_SECRET,
        "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
        "TRELLO_API_BASE": trello_base,
        "TRELLO_AUTH_BASE": trello_base,
        "TRELLO_RETRY_SECONDS": str(TRELLO_RETRY_SECONDS),
        "APP_SECRET_KEY": APP_SECRET_KEY,
    }


@dataclass
class Callback:
    """Where Trello sent Анна's browser after her decision, and the page the bot showed there."""

    url: str
    status: int
    text: str  # the page's visible text
    api_mark: int  # the Bot API call log just before the decision
    decided_at: float  # time.monotonic() just before the decision


class TrelloBot:
    """The state runningBotWithTrello and what drives it: the app, the fake Bot API, the fake Trello, Анна's browser."""

    def __init__(self, app: AppProcess, api: FakeBotApi, trello: FakeTrello, database_url: str) -> None:
        self.app = app
        self.api = api
        self.trello = trello
        self.database_url = database_url
        self.public_base_url = app.base_url  # PUBLIC_BASE_URL
        self._browser = httpx.Client(timeout=BROWSER_TIMEOUT, trust_env=False, follow_redirects=False)
        self._history: list[str] = []

    def close(self) -> None:
        self._browser.close()

    def describe(self) -> str:
        history = "\n".join(f"  {line}" for line in self._history) or "  (the browser opened nothing)"
        return (
            f"What Trello got:\n{self.trello.describe_requests()}\n"
            f"Анна's Trello:\n{self.trello.describe_state()}\n"
            f"Анна's browser:\n{history}\n"
            f"Bot API calls recorded:\n{self.api.describe_calls()}\n{self.app.output()}"
        )

    # Анна's browser

    def _request(self, method: str, url: str, data: dict[str, str] | None = None) -> httpx.Response:
        """One request of the browser; a page that cannot be loaded is an assertion failure."""
        try:
            return self._browser.request(method, url, data=data)
        except httpx.HTTPError as exc:
            self._history.append(f"{method} {url} -> {type(exc).__name__}: {exc}")
            raise AssertionError(f"Анна's browser could not load {url}: {type(exc).__name__}: {exc}") from None

    def _browse(self, method: str, url: str, data: dict[str, str] | None = None) -> httpx.Response:
        """One request, then any redirect that stays on 127.0.0.1 (at most 5)."""
        response = self._request(method, url, data=data)
        self._history.append(f"{method} {url} -> {response.status_code}")
        for _ in range(5):
            location = response.headers.get("location")
            if not response.is_redirect or not location:
                break
            target = urljoin(str(response.url), location)
            if urlsplit(target).hostname not in LOCAL_HOSTS:
                self._history.append(f"(not followed: a redirect to {target})")
                break
            response = self._request("GET", target)
            self._history.append(f"GET {target} -> {response.status_code}")
        return response

    # S06: /trello

    def send_trello_command(self) -> str:
        """Анна sends /trello; the reply must be «Чтобы подключить Trello, открой ссылку и разреши доступ: <link>».
        Returns the link."""
        delivery = deliver(self.app, self.api, command_update(ANNA, "/trello"))
        text = visible_text(delivery.reply)
        assert text.startswith(LINK_PREFIX), (
            f"Анна's /trello was answered {text!r}; expected «{LINK_PREFIX}<link>» (TRL#1)"
        )
        link = text[len(LINK_PREFIX) :]
        assert re.fullmatch(r"https?://\S+", link), (
            f"after «{LINK_PREFIX}» the reply to /trello must hold the link and nothing else; it holds {link!r}"
        )
        return link

    def open_link(self, link: str) -> AuthorizeVisit:
        """Анна's browser opens the link; it must reach Trello's authorize page with a request token Trello issued."""
        assert urlsplit(link).hostname in LOCAL_HOSTS, (
            f"the link {link} does not lead to TRELLO_AUTH_BASE ({self.trello.base_url}): the test's browser opens "
            "only local addresses"
        )
        since = self.trello.visit_mark()
        response = self._browse("GET", link)
        visits = self.trello.visits(since)
        assert visits, (
            f"opening the link {link} did not reach Trello's authorize page GET "
            f"{self.trello.base_url}/1/OAuthAuthorizeToken; the browser ended at {response.url} with "
            f"{response.status_code}"
        )
        visit = visits[-1]
        assert visit.known_token, (
            f"Trello's authorize page was opened with oauth_token={visit.oauth_token!r}, which is not a request token "
            f"Trello issued: OAuth 1.0a asks {self.trello.base_url}/1/OAuthGetRequestToken for one first, with the "
            "bot's key and its oauth_callback"
        )
        return visit

    def ask_for_trello(self) -> AuthorizeVisit:
        """State annaAskedForTrello: Анна sent /trello and her browser opened the link."""
        return self.open_link(self.send_trello_command())

    # S07: Анна allows or refuses

    def decide(self, visit: AuthorizeVisit, *, allow: bool) -> Callback:
        """Анна presses Allow or Deny on Trello's page; Trello sends her browser to the bot's callback."""
        api_mark = self.api.mark()
        decided_at = time.monotonic()
        button = {"approve": "Allow"} if allow else {"deny": "Deny"}
        url = f"{self.trello.base_url}/1/OAuthAuthorizeToken"
        answer = self._request("POST", url, data={"oauth_token": visit.oauth_token or "", **button})
        location = answer.headers.get("location")
        self._history.append(f"POST {url} ({'Allow' if allow else 'Deny'}) -> {answer.status_code} {location or ''}")
        expected = f"{self.public_base_url}{CALLBACK_PATH}"
        assert answer.status_code == 303, (
            "Trello cannot send Анна's browser back to the bot: the bot gave no oauth_callback when it asked "
            f"for the request token (TRL's seam: GET {expected}); Trello showed {page_text(answer.text)!r}"
        )
        assert location, f"Trello's redirect after Анна's decision has no Location: {answer.headers!r}"
        assert location == expected or location.startswith((f"{expected}?", f"{expected}/")), (
            f"Trello sends Анна's browser back to {location}; the bot's oauth_callback must be {expected} "
            "(TRL's seam: GET {PUBLIC_BASE_URL}/trello/callback)"
        )
        page = self._browse("GET", location)
        return Callback(location, page.status_code, page_text(page.text), api_mark, decided_at)

    def chat_after(self, callback: Callback) -> list[str]:
        """The messages Анна's chat got after her decision: waits for the first (giving up GIVE_UP_AFTER s after the
        decision), then until no further one has come for QUIET_PERIOD s."""
        return self.chat_since(callback.api_mark, callback.decided_at)

    def chat_since(self, since: int, started_at: float) -> list[str]:
        deadline = started_at + GIVE_UP_AFTER
        while not self.api.send_messages(ANNA.id, since=since) and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
        cap = time.monotonic() + GIVE_UP_AFTER  # a bot that keeps sending cannot hold the test forever
        while time.monotonic() < cap:
            calls = self.api.send_messages(ANNA.id, since=since)
            last = calls[-1].received_at if calls else started_at
            if time.monotonic() >= last + QUIET_PERIOD:
                break
            time.sleep(POLL_INTERVAL)
        return [visible_text(call) for call in self.api.send_messages(ANNA.id, since=since)]

    # the board

    def board_ready(self) -> Board | None:
        """An open board «todobot» on which all five status lists are open, if there is one."""
        for board in self.trello.boards_named(BOARD_NAME):
            if set(STATUS_LISTS) <= {x.name for x in self.trello.lists_of(board.id)}:
                return board
        return None

    def wait_for_board_ready(self) -> Board | None:
        return wait_for(self.board_ready)

    def connect_trello(self) -> Board:
        """State annaConnectedTrello: Анна sent /trello, allowed access in Trello, got a message in her chat, and her
        board «todobot» has the five lists."""
        callback = self.decide(self.ask_for_trello(), allow=True)
        reply = self.api.wait_for_send_message(
            ANNA.id, since=callback.api_mark, deadline=callback.decided_at + GIVE_UP_AFTER
        )
        assert reply is not None, (
            "while connecting Trello (state annaConnectedTrello): Анна allowed access, but no message came to her chat "
            f"within {GIVE_UP_AFTER:g} s (TRL#1: «{CONNECTED_REPLY}»); the browser showed {callback.text!r}"
        )
        board = self.wait_for_board_ready()
        assert board is not None, (
            "while connecting Trello (state annaConnectedTrello): Анна allowed access, but within "
            f"{GIVE_UP_AFTER:g} s there was no board «{BOARD_NAME}» with the lists {', '.join(STATUS_LISTS)} (TRL#2)"
        )
        return board

    # tasks and cards

    def record_task(self, text: str) -> Delivery:
        """Анна writes `text` (a plain text message: a task, CAP#1); the reply must be «Сохранил ✅»."""
        delivery = deliver(self.app, self.api, text_update(ANNA, text))
        assert delivery.reply_text == SAVED_REPLY, (
            f"Анна's task {text!r} was answered {delivery.reply_text!r}; expected «{SAVED_REPLY}» (CAP#1, TRL#3)"
        )
        return delivery

    def wait_for_cards(self, count: int, *, include_closed: bool = False) -> list[Card]:
        """The cards on the board «todobot» once there are at least `count`, or whatever is there after
        GIVE_UP_AFTER s; in the order they were made."""

        def enough() -> list[Card]:
            cards = self.trello.cards_on(include_closed=include_closed)
            return cards if len(cards) >= count else []

        return wait_for(enough) or self.trello.cards_on(include_closed=include_closed)

    def cards(self, *, include_closed: bool = False) -> list[Card]:
        return self.trello.cards_on(include_closed=include_closed)

    def describe_cards(self, cards: list[Card]) -> str:
        if not cards:
            return "(no cards)"
        return "; ".join(f"«{c.name}» in «{self.trello.list_name(c.id_list)}» desc={c.desc!r}" for c in cards)


@contextmanager
def running_bot_with_trello(database_url: str, fake_api: FakeBotApi) -> Iterator[TrelloBot]:
    """The state runningBotWithTrello: a fake Trello, then `migrate` and `serve` with SK's environment plus TRL's
    (TRELLO_API_KEY, TRELLO_API_SECRET, PUBLIC_BASE_URL, TRELLO_API_BASE and TRELLO_AUTH_BASE at the fake,
    TRELLO_RETRY_SECONDS=1, APP_SECRET_KEY), with no allow list and no PostHog whatever the caller's shell holds.
    Анна has not connected Trello. An assertion failing inside gets a note with what Trello got, Анна's Trello, her
    browser's history, the Bot API calls and the app's output. Everything is stopped on the way out."""
    with FakeTrello() as trello:
        port = free_port()
        env = app_env(database_url=database_url, port=port, api_base=fake_api.base_url)
        for name in (*CAP_NOT_INHERITED, *TRELLO_ENV_VARS):
            env.pop(name, None)
        env.update(trello_env(trello.base_url, port))
        run_migrate(env)
        app = AppProcess(env, port)
        bot = TrelloBot(app, fake_api, trello, database_url)
        try:
            app.start()
            yield bot
        except AssertionError as exc:
            exc.add_note(bot.describe())
            raise
        finally:
            bot.close()
            app.stop()
