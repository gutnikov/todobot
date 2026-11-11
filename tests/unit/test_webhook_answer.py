"""The webhook's answer, in-process: the real app on a real Postgres, with a recording fake Bot API.

- DUP#2: the 200 comes only once the update is handled: the message stored and the reply sent, or the apology tried.
  Shutting down, an update in flight is finished and answered; one that is not finished in time gets no answer.
- SK's seams: every POST with the right secret gets 200 `{}`, also when handling fails; a reply is a Bot API call,
  never the response body.
- DUP#1 through the webhook: Telegram's repeat of an update is kept once and not replied to again.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import psycopg
from aiogram import Bot, Dispatcher, Router
from aiogram.methods import SendMessage
from aiogram.types import Message
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from fakes import HEADER, SECRET, TOKEN, VERSION, FakeBotApi, app_settings, logs_on_stdout, message_update, running_app

from todobot import texts
from todobot.bot import build_bot
from todobot.web import SHUTDOWN_TIMEOUT_SECONDS, WEBHOOK_PATH, WebhookHandler, build_app

USER = 555_555_555
TEXT = "QK4-тайна: забрать посылку"  # the marker is ASCII: the JSON lines escape Cyrillic


def _inbox(database_url: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(database_url) as conn:
        return conn.execute("SELECT user_id, message_id, kind, text FROM inbox ORDER BY received_at").fetchall()


def _records(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines()]


async def _held_until_released(client: TestClient[Any, Any], api: FakeBotApi, update: dict[str, Any]) -> int:
    """POST `update` while the Bot API holds sendMessage: the webhook must not answer before the reply is through."""
    post = asyncio.create_task(client.post(WEBHOOK_PATH, json=update, headers={HEADER: SECRET}))
    await api.first_call()  # the reply (or the apology) reached the Bot API, which holds it
    await asyncio.sleep(0.3)
    assert not post.done(), "the webhook answered while the reply was still on its way"
    api.release.set()
    response = await post
    assert await response.json() == {}
    return response.status


def test_the_answer_comes_after_the_message_is_stored_and_the_reply_sent(migrated_database_url: str) -> None:
    update = message_update(USER, text=TEXT)

    async def scenario() -> tuple[int, list[Any]]:
        async with running_app(migrated_database_url, FakeBotApi(hold=True)) as (client, api):
            return await _held_until_released(client, api, update), api.calls

    status, calls = asyncio.run(scenario())

    assert status == 200
    assert calls == [("sendMessage", {"chat_id": str(USER), "text": texts.SAVED})]
    assert _inbox(migrated_database_url) == [(USER, update["message"]["message_id"], "text", TEXT)]


def test_the_answer_comes_after_the_apology_when_storing_fails(database_url: str) -> None:
    """SK-5's trigger: the database is not migrated, so storing fails."""

    async def scenario() -> tuple[int, list[Any]]:
        async with running_app(database_url, FakeBotApi(hold=True)) as (client, api):
            return await _held_until_released(client, api, message_update(USER, text=TEXT)), api.calls

    status, calls = asyncio.run(scenario())

    assert status == 200
    assert calls == [("sendMessage", {"chat_id": str(USER), "text": texts.ERROR})]


def test_a_repeated_update_is_kept_once_and_not_replied_to_again(migrated_database_url: str, capsys) -> None:
    update = message_update(USER, text=TEXT)
    message_id = update["message"]["message_id"]

    async def scenario() -> tuple[list[int], list[Any]]:
        async with running_app(migrated_database_url) as (client, api):
            statuses = [(await client.post(WEBHOOK_PATH, json=update, headers={HEADER: SECRET})).status for _ in "12"]
            return statuses, api.calls

    with logs_on_stdout():
        statuses, calls = asyncio.run(scenario())
    stdout = capsys.readouterr().out

    assert statuses == [200, 200]
    assert calls == [("sendMessage", {"chat_id": str(USER), "text": texts.SAVED})]
    assert _inbox(migrated_database_url) == [(USER, message_id, "text", TEXT)]
    records = _records(stdout)
    saved = [record for record in records if record["event"] == "inbox_saved"]
    repeats = [record for record in records if record["event"] == "inbox_repeat"]
    assert len(saved) == 1, stdout
    assert len(repeats) == 1, stdout
    assert {key: repeats[0][key] for key in ("level", "version", "user_id", "message_id", "kind")} == {
        "level": "INFO",
        "version": VERSION,
        "user_id": USER,
        "message_id": message_id,
        "kind": "text",
    }
    assert "QK4" not in stdout


def test_reply_and_apology_refused_still_get_200(migrated_database_url: str, capsys) -> None:
    """Telegram refuses every call: the message is stored, the reply and the apology fail, and the answer is 200."""

    async def scenario() -> int:
        async with running_app(migrated_database_url, FakeBotApi(refuse=True)) as (client, _):
            response = await client.post(WEBHOOK_PATH, json=message_update(USER, text=TEXT), headers={HEADER: SECRET})
            return response.status

    with logs_on_stdout():
        status = asyncio.run(scenario())
    events = [record["event"] for record in _records(capsys.readouterr().out)]

    assert status == 200
    assert [row[0] for row in _inbox(migrated_database_url)] == [USER]
    assert "handler_error" in events
    assert "apology_failed" in events


async def _post_to_handler(dispatcher: Dispatcher, body: Any) -> tuple[aiohttp.ClientResponse, Any, FakeBotApi]:
    """POST `body` (JSON, or raw bytes) to a WebhookHandler over `dispatcher`; its bot calls the fake Bot API."""
    api = FakeBotApi()
    async with TestServer(api.app) as api_server:
        bot = build_bot(TOKEN, str(api_server.make_url("")))
        app = web.Application()
        WebhookHandler(dispatcher, bot, secret_token=SECRET).register(app, path=WEBHOOK_PATH)
        async with TestClient(TestServer(app)) as client:
            payload: dict[str, Any] = {"data": body} if isinstance(body, bytes) else {"json": body}
            response = await client.post(WEBHOOK_PATH, headers={HEADER: SECRET}, **payload)
            return response, await response.read(), api


def test_a_method_a_handler_returns_is_called_not_put_into_the_answer() -> None:
    router = Router()

    @router.message()
    async def reply_by_returning(message: Message) -> SendMessage:
        return message.answer("ответ")  # the method, not called

    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    response, body, api = asyncio.run(_post_to_handler(dispatcher, message_update(USER, text="привет")))

    assert response.status == 200
    assert response.content_type == "application/json"
    assert json.loads(body) == {}
    assert api.calls == [("sendMessage", {"chat_id": str(USER), "text": "ответ"})]


class _BrokenDispatcher(Dispatcher):
    async def feed_raw_update(self, bot: Bot, update: dict[str, Any], **kwargs: Any) -> Any:
        raise RuntimeError("QJ3-тайна from the depths")


def test_a_dispatcher_that_raises_still_gets_200_and_one_line_without_its_message(capsys) -> None:
    with logs_on_stdout():
        response, body, api = asyncio.run(_post_to_handler(_BrokenDispatcher(), {"update_id": 91}))
    stdout = capsys.readouterr().out

    assert response.status == 200
    assert json.loads(body) == {}
    assert api.calls == []
    failed = [record for record in _records(stdout) if record["event"] == "update_failed"]
    assert len(failed) == 1, stdout
    assert (failed[0]["level"], failed[0]["update_id"], failed[0]["error"]) == ("ERROR", 91, "RuntimeError")
    assert "QJ3" not in stdout


def test_a_body_that_is_not_json_gets_200_and_one_update_invalid_line(capsys) -> None:
    with logs_on_stdout():
        response, body, api = asyncio.run(_post_to_handler(Dispatcher(), b"QH2 not json"))
    stdout = capsys.readouterr().out

    assert response.status == 200
    assert json.loads(body) == {}
    assert api.calls == []
    invalid = [record for record in _records(stdout) if record["event"] == "update_invalid"]
    assert len(invalid) == 1, stdout
    assert (invalid[0]["level"], invalid[0]["error"]) == ("ERROR", "JSONDecodeError")
    assert "QH2" not in stdout


async def _shut_down_while_replying(
    database_url: str, update: dict[str, Any], *, shutdown_timeout: float, release_after: float | None
) -> tuple[int | None, list[Any]]:
    """POST `update`; once its reply reached the Bot API (held there), shut the app down with aiohttp's
    `shutdown_timeout`; release the reply `release_after` s into the shutdown (None: never). The webhook's status, or
    None when the connection closed without an answer."""
    api = FakeBotApi(hold=True)
    async with TestServer(api.app) as api_server:
        server = TestServer(build_app(app_settings(database_url, api_server)))
        await server.start_server(shutdown_timeout=shutdown_timeout)
        async with TestClient(server) as client:
            post = asyncio.create_task(client.post(WEBHOOK_PATH, json=update, headers={HEADER: SECRET}))
            await api.first_call()
            closing = asyncio.create_task(server.close())
            if release_after is not None:
                await asyncio.sleep(release_after)
                assert not closing.done(), "shutdown did not wait for the update in flight"
                api.release.set()
            try:
                status: int | None = (await post).status
            except aiohttp.ClientError:
                status = None
            await closing
        return status, api.calls


def test_shutdown_finishes_an_update_in_flight_and_answers_it(migrated_database_url: str, capsys) -> None:
    update = message_update(USER, text=TEXT)

    with logs_on_stdout():
        status, calls = asyncio.run(
            _shut_down_while_replying(
                migrated_database_url, update, shutdown_timeout=SHUTDOWN_TIMEOUT_SECONDS, release_after=0.5
            )
        )
    records = _records(capsys.readouterr().out)

    assert status == 200
    assert calls == [("sendMessage", {"chat_id": str(USER), "text": texts.SAVED})]
    assert [record for record in records if record["level"] in ("ERROR", "CRITICAL")] == []
    assert len(_inbox(migrated_database_url)) == 1


def test_an_update_not_finished_by_shutdown_gets_no_answer_and_its_repeat_is_kept_once(
    migrated_database_url: str,
) -> None:
    update = message_update(USER, text=TEXT)

    status, _ = asyncio.run(
        _shut_down_while_replying(migrated_database_url, update, shutdown_timeout=0.3, release_after=None)
    )
    assert status is None, "an update the bot did not finish handling must get no answer, so Telegram sends it again"

    async def telegram_sends_it_again() -> tuple[int, list[Any]]:
        async with running_app(migrated_database_url) as (client, api):
            response = await client.post(WEBHOOK_PATH, json=update, headers={HEADER: SECRET})
            return response.status, api.calls

    status, calls = asyncio.run(telegram_sends_it_again())
    assert status == 200
    assert calls == []  # stored before the shutdown cut the reply short: a repeat, not replied to again
    assert len(_inbox(migrated_database_url)) == 1
