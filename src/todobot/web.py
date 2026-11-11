"""The HTTP server: the Telegram webhook and `GET /healthz`, on one aiohttp app."""

from __future__ import annotations

import asyncio
import logging
import secrets
import signal
from collections.abc import Callable

from aiogram import Bot, Dispatcher
from aiogram.methods import TelegramMethod
from aiohttp import web
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from todobot.bot import build_bot, build_dispatcher, update_id_of
from todobot.config import Settings
from todobot.db import create_engine
from todobot.logs import log_event

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram/webhook"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 - the header's name, not a secret
HEALTH_PATH = "/healthz"
HOST = "0.0.0.0"  # noqa: S104 - the contract: serve listens on 0.0.0.0:PORT

# How long /healthz waits for the database's answer to `SELECT 1` before it reports 503.
HEALTH_TIMEOUT_SECONDS = 3.0
# How long shutdown waits, in all, for updates that are still being handled. One not done by then is cancelled and
# gets no answer, so Telegram sends it again (DUP#2).
SHUTDOWN_GRACE_SECONDS = 5.0
# aiohttp's `shutdown_timeout`: aiohttp (3.14, RequestHandler.shutdown) waits this long for a request in flight, then
# as long again before it cancels it.
SHUTDOWN_TIMEOUT_SECONDS = SHUTDOWN_GRACE_SECONDS / 2

ENGINE = web.AppKey("engine", AsyncEngine)
VERSION = web.AppKey("version", str)


class WebhookHandler:
    """`POST /telegram/webhook`: an update with the right secret is handled to the end, then answered 200 `{}`.

    DUP#2: the answer comes only after the update is handled, the message stored and the reply sent, or, when
    handling fails, the apology tried. An update the process does not finish (it stops first) gets no answer, so
    Telegram sends it again, and the inbox keeps it once (DUP#1). The 200 does not depend on the handler succeeding:
    `TodobotDispatcher.feed_raw_update` raises nothing, and anything else that does is logged here without content.

    aiogram's SimpleRequestHandler cannot be set up so: in the background it answers before the update is handled;
    otherwise it puts a method a handler returns into the response body, answers a handler slower than 55 s early and
    lets it run on, and validates the update itself, so a ValidationError, which quotes the user's text, escapes into
    aiohttp's error log. Here a reply is always a real Bot API call: a method a handler returns is called, and the
    response body is always `{}`.
    """

    def __init__(self, dispatcher: Dispatcher, bot: Bot, *, secret_token: str) -> None:
        if not secret_token:
            raise ValueError("the webhook secret must not be empty")
        self.dispatcher = dispatcher
        self.bot = bot
        self._secret = secret_token.encode()

    def register(self, app: web.Application, *, path: str) -> None:
        app.router.add_post(path, self.handle)
        # On cleanup, not on shutdown: aiohttp runs cleanup after it has waited for the requests in flight, whose
        # replies need the Bot API session.
        app.on_cleanup.append(self._close_session)

    def verify_secret(self, telegram_secret_token: str) -> bool:
        # Fails closed, takes any header (aiogram's own check raises on a non-ASCII one), compares in constant time.
        given = telegram_secret_token.encode("utf-8", "surrogatepass")
        return bool(self._secret) and secrets.compare_digest(given, self._secret)

    async def handle(self, request: web.Request) -> web.Response:
        if not self.verify_secret(request.headers.get(SECRET_HEADER, "")):
            return web.Response(status=401, text="Unauthorized")
        try:
            update = await request.json()
        except ValueError as exc:  # Telegram sends JSON; a body that is not would not be one on a second try either
            log_event(
                logger,
                "update_invalid",
                level=logging.ERROR,
                update_id=None,
                error=type(exc).__name__,
                problems=["update: json_invalid"],  # as TodobotDispatcher puts them: where and what, not the input
            )
            return web.json_response({})
        try:
            result = await self.dispatcher.feed_raw_update(self.bot, update)
            if isinstance(result, TelegramMethod):
                await self.dispatcher.silent_call_request(self.bot, result)
        except Exception as exc:  # the type only: a traceback could quote what the user wrote
            log_event(
                logger, "update_failed", level=logging.ERROR, update_id=update_id_of(update), error=type(exc).__name__
            )
        return web.json_response({})

    async def _close_session(self, app: web.Application) -> None:
        await self.bot.session.close()


async def healthz(request: web.Request) -> web.Response:
    """SK-4: 200 with the version when the database answers `SELECT 1`, else 503."""
    version = request.app[VERSION]
    try:
        async with asyncio.timeout(HEALTH_TIMEOUT_SECONDS):
            async with request.app[ENGINE].connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception as exc:  # any failure to get the answer means the database does not answer
        log_event(logger, "healthz_failed", level=logging.WARNING, error=f"{type(exc).__name__}: {exc}")
        return web.json_response({"status": "error", "version": version}, status=503)
    return web.json_response({"status": "ok", "version": version})


def build_app(settings: Settings) -> web.Application:
    """The aiohttp app. Building and starting it touches neither the database nor the Bot API."""
    engine = create_engine(settings.database_url)
    bot = build_bot(settings.bot_token, settings.api_base)
    dispatcher = build_dispatcher(engine)

    app = web.Application()
    app[ENGINE] = engine
    app[VERSION] = settings.version
    app.router.add_get(HEALTH_PATH, healthz)
    WebhookHandler(dispatcher, bot, secret_token=settings.webhook_secret).register(app, path=WEBHOOK_PATH)

    async def dispose_engine(app: web.Application) -> None:
        await app[ENGINE].dispose()

    app.on_cleanup.append(dispose_engine)
    return app


async def serve(settings: Settings, *, on_started: Callable[[], None] | None = None) -> None:
    """Serve on 0.0.0.0:PORT until SIGTERM or SIGINT, then shut down gracefully.

    `on_started` is called once the server listens (ERR#2: it tells PostHog).

    Shutting down, the server stops taking requests, waits up to SHUTDOWN_GRACE_SECONDS for the updates in flight to
    be handled and answered, cancels the rest (unanswered, Telegram sends them again), and then closes the Bot API
    session and the database engine.
    """
    runner = web.AppRunner(build_app(settings), shutdown_timeout=SHUTDOWN_TIMEOUT_SECONDS)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host=HOST, port=settings.port)
        await site.start()
        log_event(logger, "server_started", host=HOST, port=settings.port)
        if on_started is not None:
            on_started()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        log_event(logger, "server_stopping")
    finally:
        await runner.cleanup()
    log_event(logger, "server_stopped")
