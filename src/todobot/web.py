"""The HTTP server: the Telegram webhook and `GET /healthz`, on one aiohttp app."""

from __future__ import annotations

import asyncio
import logging
import secrets
import signal

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from todobot.bot import build_bot, build_dispatcher
from todobot.config import Settings
from todobot.db import create_engine
from todobot.logs import log_event

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram/webhook"
HEALTH_PATH = "/healthz"
HOST = "0.0.0.0"  # noqa: S104 - the contract: serve listens on 0.0.0.0:PORT

# How long /healthz waits for the database's answer to `SELECT 1` before it reports 503.
HEALTH_TIMEOUT_SECONDS = 3.0
# How long shutdown waits for updates that are still being handled.
SHUTDOWN_GRACE_SECONDS = 5.0

ENGINE = web.AppKey("engine", AsyncEngine)
VERSION = web.AppKey("version", str)


class WebhookHandler(SimpleRequestHandler):
    """aiogram's webhook handler, set up for this bot.

    It answers each accepted update at once with 200 and `{}`, and handles the update in the background
    (`handle_in_background=True`). So a reply is always a real Bot API call (`sendMessage`): even a handler that
    returns a method instead of calling it gets it called, never put into the webhook's response body (that only
    happens with `handle_in_background=False`). And the 200 does not depend on the handler succeeding.
    """

    def __init__(self, dispatcher: Dispatcher, bot: Bot, *, secret_token: str) -> None:
        if not secret_token:
            raise ValueError("the webhook secret must not be empty")
        super().__init__(dispatcher, bot, handle_in_background=True, secret_token=secret_token)

    def verify_secret(self, telegram_secret_token: str, bot: Bot) -> bool:
        # aiogram's own check passes everything when no secret is set, and raises on a non-ASCII header.
        # This one fails closed and compares bytes in constant time.
        expected = (self.secret_token or "").encode()
        given = telegram_secret_token.encode("utf-8", "surrogatepass")
        return bool(expected) and secrets.compare_digest(given, expected)

    async def close(self) -> None:
        # Let updates already accepted finish (store and reply) before the Bot API session closes.
        # `_background_feed_update_tasks` is aiogram's; its version is pinned in uv.lock.
        pending = set(self._background_feed_update_tasks)
        if pending:
            await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)
        await super().close()


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


async def serve(settings: Settings) -> None:
    """Serve on 0.0.0.0:PORT until SIGTERM or SIGINT, then shut down gracefully."""
    runner = web.AppRunner(build_app(settings))
    await runner.setup()
    try:
        site = web.TCPSite(runner, host=HOST, port=settings.port)
        await site.start()
        log_event(logger, "server_started", host=HOST, port=settings.port)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        log_event(logger, "server_stopping")
    finally:
        await runner.cleanup()
    log_event(logger, "server_stopped")
