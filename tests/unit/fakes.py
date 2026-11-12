"""Shared by the unit tests: Telegram updates, a recording fake Bot API, a recording fake PostHog, and the app running
in-process."""

from __future__ import annotations

import asyncio
import gc
import gzip
import itertools
import json
import logging
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import trello_fake
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from todobot.config import Settings, TrelloSettings
from todobot.logs import JsonFormatter
from todobot.web import AccessLog, build_app

TOKEN = "123456789:unit-test-token"
SECRET = "unit_webhook_secret_9f2c"
HEADER = "X-Telegram-Bot-Api-Secret-Token"
VERSION = "unit"
# TRL: the bot's address as Trello's callback names it (the tests call the app's client instead), and a Fernet key.
PUBLIC_BASE_URL = "https://bot.example"
APP_SECRET_KEY = "dW5pdC10ZXN0cy1hcHAtc2VjcmV0LWtleS0zMmJ5dGU="

_ids = itertools.count(1)


def message_update(user_id: int, **content: Any) -> dict[str, Any]:
    """An Update with a private-chat message from `user_id`; `content` is the message's payload."""
    return {
        "update_id": next(_ids),
        "message": {
            "message_id": next(_ids),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "chat": {"id": user_id, "type": "private", "first_name": "Test"},
            "date": int(time.time()),
            **content,
        },
    }


class FakeBotApi:
    """Records `/bot<token>/<method>` calls; a Message for sendMessage, `true` for anything else.

    `hold=True`: a sendMessage call is recorded when it arrives and answered only once `release` is set.
    `refuse=True`: every call is answered with Telegram's 400, as when it refuses a message.
    """

    def __init__(self, *, hold: bool = False, refuse: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.called = asyncio.Event()
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.refuse = refuse
        self.app = web.Application()
        self.app.router.add_post("/bot{token}/{method}", self.handle)

    async def handle(self, request: web.Request) -> web.Response:
        form = {key: str(value) for key, value in (await request.post()).items()}
        method = request.match_info["method"]
        self.calls.append((method, form))
        self.called.set()
        if self.refuse:
            refusal = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
            return web.json_response(refusal, status=400)
        result: Any = True
        if method == "sendMessage":
            await self.release.wait()
            chat = {"id": int(form["chat_id"]), "type": "private"}
            result = {"message_id": len(self.calls), "date": int(time.time()), "chat": chat, "text": form["text"]}
        return web.json_response({"ok": True, "result": result})

    async def first_call(self) -> None:
        async with asyncio.timeout(10):
            await self.called.wait()


def app_settings(
    database_url: str,
    api_server: TestServer,
    *,
    allowed_user_ids: frozenset[int] | None = None,
    trello: TrelloSettings | None = None,
) -> Settings:
    """What `serve` would get, with the Bot API at `api_server`; open to every user unless `allowed_user_ids`; with
    Trello only with `trello`."""
    return Settings(
        database_url=database_url,
        bot_token=TOKEN,
        webhook_secret=SECRET,
        api_base=str(api_server.make_url("")),
        version=VERSION,
        allowed_user_ids=allowed_user_ids,
        trello=trello,
    )


def trello_settings(trello_server: TestServer, *, retry_seconds: float = 3600.0) -> TrelloSettings:
    """TRL's settings with Trello (OAuth and REST) at `trello_server`, a FakeTrello's. The loop's tick is long by
    default: a round runs at start and when nudged."""
    base = str(trello_server.make_url("")).rstrip("/")
    return TrelloSettings(
        api_key=trello_fake.KEY,
        api_secret=trello_fake.SECRET,
        public_base_url=PUBLIC_BASE_URL,
        secret_key=APP_SECRET_KEY,
        api_base=base,
        auth_base=base,
        retry_seconds=retry_seconds,
    )


@asynccontextmanager
async def running_app(
    database_url: str,
    api: FakeBotApi | None = None,
    *,
    allowed_user_ids: frozenset[int] | None = None,
    trello: TrelloSettings | None = None,
) -> AsyncIterator[tuple[TestClient[Any, Any], FakeBotApi]]:
    """The real app (`build_app`) on `database_url`, its Bot API calls going to `api` (a fresh FakeBotApi), serving
    only `allowed_user_ids` when given (ACC), with Trello when `trello` says where (TRL). Its access log is `serve`'s.
    """
    api = api or FakeBotApi()
    async with TestServer(api.app) as api_server:
        settings = app_settings(database_url, api_server, allowed_user_ids=allowed_user_ids, trello=trello)
        server = TestServer(build_app(settings))
        await server.start_server(access_log_class=AccessLog)
        async with TestClient(server) as client:
            yield client, api


async def eventually[T](probe: Callable[[], T], seconds: float = 10.0) -> T:
    """`probe()` once it is truthy; a TimeoutError after `seconds`. The fakes keep state, not events: this polls."""
    deadline = time.monotonic() + seconds
    for _ in itertools.count():
        if value := probe():
            return value
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(0.02)
    raise TimeoutError(f"not within {seconds:g} s")


@contextmanager
def logs_on_stdout() -> Iterator[None]:
    """Log as `serve` does (the production formatter, on stdout, from INFO), for capsys to read."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(VERSION))
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield
        gc.collect()  # asyncio logs a failed task nobody awaited when it is collected: make that happen in here
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


class FakePostHog:
    """A local PostHog: records every request's body, and answers 200 (takes), 500 (`refuse=True`), or not at all
    until the test is over (`hang=True`). The SDK posts from a thread of its own, so this one serves from a thread."""

    def __init__(self, *, refuse: bool = False, hang: bool = False) -> None:
        self.bodies: list[bytes] = []
        self.statuses: list[int] = []
        self._over = threading.Event()
        posthog = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                status = 500 if refuse else 200
                posthog.bodies.append(body)
                posthog.statuses.append(status)
                if hang:
                    posthog._over.wait(30)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "13")
                self.end_headers()
                self.wfile.write(b'{"status": 1}')

            def log_message(self, *args: Any) -> None:  # not on stderr
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, name="fake-posthog", daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    @property
    def events(self) -> list[dict[str, Any]]:
        """Every event of every request, in order (a body is a batch: `{"batch": [...]}`)."""
        return [event for body in self.bodies for event in json.loads(body)["batch"]]

    def __enter__(self) -> FakePostHog:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._over.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
