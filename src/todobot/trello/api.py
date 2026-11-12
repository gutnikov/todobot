"""Trello's HTTP API as the bot uses it: OAuth 1.0a (TRL#1) and the REST calls for boards, lists and cards.

Every call is signed with OAuth 1.0a, HMAC-SHA1 (oauthlib), in the `Authorization` header: neither the bot's key nor
a user's token is ever part of a URL. A GET's parameters go in the query (ids and field names only); what a POST sends
(a card's name and description, what the user wrote) goes in a JSON body, which the signature does not cover (RFC 5849
3.4.1.3.1 counts only a form-encoded body). A call that fails is a TrelloError naming the call and Trello's status or
the error's type, never what was sent or what Trello answered.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Literal
from urllib.parse import parse_qs, quote, urlencode

import aiohttp
from oauthlib.oauth1 import Client
from yarl import URL

from todobot.config import TrelloSettings

REQUEST_TOKEN_PATH = "/1/OAuthGetRequestToken"  # noqa: S105 - a path, not a secret
AUTHORIZE_PATH = "/1/OAuthAuthorizeToken"
ACCESS_TOKEN_PATH = "/1/OAuthGetAccessToken"  # noqa: S105 - a path, not a secret

# What the authorize page asks the user for (TRL#1): read and write access, with no end date, for the app "todobot".
APP_NAME = "todobot"
SCOPE = "read,write"
EXPIRATION = "never"

HttpMethod = Literal["GET", "POST"]

# How long one call to Trello may take, all in all. /tasks waits for Trello within the webhook, so it is bounded.
TIMEOUT_SECONDS = 10.0


class TrelloError(Exception):
    """A call to Trello that did not succeed: which call, and Trello's HTTP status or the error's type.

    Nothing of what was sent (a token, what the user wrote) or answered is in it: its message gets logged.
    """

    def __init__(self, call: str, *, status: int | None = None, error: str | None = None) -> None:
        self.call = call
        self.status = status
        self.error = error
        super().__init__(f"{call}: HTTP {status}" if status is not None else f"{call}: {error}")


@dataclass(frozen=True)
class Access:
    """A user's access to their Trello: the OAuth access token and its secret. Its repr shows neither."""

    token: str = field(repr=False)
    secret: str = field(repr=False)

    def dumps(self) -> str:
        return json.dumps({"token": self.token, "secret": self.secret})

    @classmethod
    def loads(cls, raw: str) -> Access | None:
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(value, dict):
            return None
        token, secret = value.get("token"), value.get("secret")
        if not isinstance(token, str) or not isinstance(secret, str) or not token:
            return None
        return cls(token=token, secret=secret)


def oauth_header(
    method: HttpMethod,
    url: str,
    *,
    key: str,
    secret: str,
    token: str | None = None,
    token_secret: str | None = None,
    callback: str | None = None,
    verifier: str | None = None,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """The `Authorization: OAuth …` value of a request to `url` (its query included in the signature), signed with
    HMAC-SHA1 by the consumer `key`/`secret` and, once there is one, the `token` and its secret. `nonce` and
    `timestamp` are for tests; by default they are fresh."""
    client = Client(
        key,
        client_secret=secret,
        resource_owner_key=token,
        resource_owner_secret=token_secret,
        callback_uri=callback,
        verifier=verifier,
        nonce=nonce,
        timestamp=timestamp,
    )
    _, headers, _ = client.sign(url, http_method=method)
    return str(headers["Authorization"])


def _query(params: Mapping[str, str] | None) -> str:
    return f"?{urlencode(dict(params), quote_via=quote)}" if params else ""


class TrelloApi:
    """One HTTP session to Trello for the life of `serve`: `start` it inside the event loop, `close` it on the way
    out (or use it as an async context manager)."""

    def __init__(self, settings: TrelloSettings) -> None:
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> TrelloApi:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    # OAuth 1.0a (TRL#1)

    async def request_token(self) -> tuple[str, str]:
        """A request token and its secret, Trello to call the bot back at `{PUBLIC_BASE_URL}/trello/callback`."""
        url = f"{self.settings.auth_base}{REQUEST_TOKEN_PATH}"
        authorization = self._sign("POST", url, callback=self.settings.callback_url)
        answer = _form(await self._send("POST", url, authorization), REQUEST_TOKEN_PATH)
        return answer["oauth_token"], answer["oauth_token_secret"]

    def authorize_url(self, request_token: str) -> str:
        """Trello's page that asks the user to allow access: read and write, with no end date."""
        query = {"oauth_token": request_token, "name": APP_NAME, "scope": SCOPE, "expiration": EXPIRATION}
        return f"{self.settings.auth_base}{AUTHORIZE_PATH}?{urlencode(query, safe=',')}"

    async def access_token(self, request_token: str, request_secret: str, verifier: str) -> Access:
        """The user's access, for the request token they allowed and the verifier Trello sent back with them."""
        url = f"{self.settings.auth_base}{ACCESS_TOKEN_PATH}"
        authorization = self._sign("POST", url, token=request_token, token_secret=request_secret, verifier=verifier)
        answer = _form(await self._send("POST", url, authorization), ACCESS_TOKEN_PATH)
        return Access(token=answer["oauth_token"], secret=answer["oauth_token_secret"])

    # REST, on a user's behalf

    async def get(self, access: Access, path: str, params: Mapping[str, str] | None = None) -> Any:
        url = f"{self.settings.api_base}{path}{_query(params)}"
        authorization = self._sign("GET", url, token=access.token, token_secret=access.secret)
        return _json(await self._send("GET", url, authorization), f"GET {path}")

    async def post(self, access: Access, path: str, body: Mapping[str, Any]) -> Any:
        url = f"{self.settings.api_base}{path}"
        authorization = self._sign("POST", url, token=access.token, token_secret=access.secret)
        return _json(await self._send("POST", url, authorization, body), f"POST {path}")

    # the wire

    def _sign(self, method: HttpMethod, url: str, **oauth: str | None) -> str:
        return oauth_header(method, url, key=self.settings.api_key, secret=self.settings.api_secret, **oauth)

    async def _send(
        self, method: HttpMethod, url: str, authorization: str, body: Mapping[str, Any] | None = None
    ) -> bytes:
        call = f"{method} {URL(url).path}"
        if self._session is None:
            raise RuntimeError("TrelloApi is not started")
        try:
            # encoded=True: the URL goes out exactly as it was signed.
            async with self._session.request(
                method, URL(url, encoded=True), headers={"Authorization": authorization}, json=body
            ) as response:
                status = response.status
                content = await response.read()
        except (aiohttp.ClientError, TimeoutError) as exc:  # its message can hold the URL: the type only
            raise TrelloError(call, error=type(exc).__name__) from None
        if not 200 <= status < 300:
            raise TrelloError(call, status=status)
        return content


def _form(content: bytes, call: str) -> dict[str, str]:
    """An OAuth endpoint's answer: `oauth_token` and `oauth_token_secret`, form-encoded."""
    try:
        values = {name: value[-1] for name, value in parse_qs(content.decode("utf-8"), strict_parsing=True).items()}
    except (UnicodeDecodeError, ValueError):
        raise TrelloError(call, error="UnexpectedAnswer") from None
    if not values.get("oauth_token") or not values.get("oauth_token_secret"):
        raise TrelloError(call, error="UnexpectedAnswer")
    return values


def _json(content: bytes, call: str) -> Any:
    try:
        return json.loads(content)
    except ValueError:
        raise TrelloError(call, error="UnexpectedAnswer") from None
