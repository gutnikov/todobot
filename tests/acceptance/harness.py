"""Black-box harness for the todobot acceptance tests.

Built only to the "Test seams" contract of agentify/specs/requirements/skeleton.md:
the app is started with `uv run python -m todobot migrate|serve`, configured by
environment variables, fed Telegram Updates over `POST /telegram/webhook`, and its
replies are read from a fake Telegram Bot API. Nothing here imports the app.
"""

from __future__ import annotations

import email.parser
import email.policy
import itertools
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- The bot's texts, byte for byte from the requirement ----------------------------

START_REPLY = "Привет! Присылай задачи текстом, голосом или картинкой — я их сохраню."
SAVED_REPLY = "Сохранил ✅"
ERROR_REPLY = "Что-то пошло не так, попробуй ещё раз."

# --- What the tests configure the app with ------------------------------------------

APP_VERSION = "sk-acceptance-7f3c9e1"
BOT_ID = 7012345678
BOT_TOKEN = f"{BOT_ID}:AAHfakeAcceptanceTokenNotReal_01234"
WEBHOOK_SECRET = "acceptance_webhook_secret_5b8d2f"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
WEBHOOK_PATH = "/telegram/webhook"

APP_START_TIMEOUT = 20.0  # seconds until `serve` must accept connections
MIGRATE_TIMEOUT = 120.0  # the first `uv run` may have to build the project
STOP_TIMEOUT = 10.0
DEFAULT_REPLY_TIMEOUT = 10.0  # for criteria without their own limit

CONTRACT_ENV_VARS = (
    "DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "TELEGRAM_API_BASE",
    "PORT",
    "APP_VERSION",
)


def free_port() -> int:
    """A TCP port on 127.0.0.1 that nothing listens on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def with_database(url: str, database: str) -> str:
    """The same server URL pointing at another database."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


# --- Telegram Updates ----------------------------------------------------------------

_update_ids = itertools.count(815_204_001)
_message_ids = itertools.count(1001)
_file_seq = itertools.count(1)


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str
    username: str | None = None
    language_code: str = "ru"

    def as_user(self) -> dict[str, Any]:
        user: dict[str, Any] = {"id": self.id, "is_bot": False, "first_name": self.first_name}
        if self.username:
            user["username"] = self.username
        user["language_code"] = self.language_code
        return user

    def as_private_chat(self) -> dict[str, Any]:
        chat: dict[str, Any] = {"id": self.id, "first_name": self.first_name}
        if self.username:
            chat["username"] = self.username
        chat["type"] = "private"
        return chat


def message_update(user: TelegramUser, **content: Any) -> dict[str, Any]:
    """An Update with a private-chat message from `user`; `content` is the message's payload."""
    return {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_message_ids),
            "from": user.as_user(),
            "chat": user.as_private_chat(),
            "date": int(time.time()),
            **content,
        },
    }


def text_update(user: TelegramUser, text: str) -> dict[str, Any]:
    return message_update(user, text=text)


def command_update(user: TelegramUser, command: str) -> dict[str, Any]:
    """A bot command as Telegram sends it: the text plus a bot_command entity at offset 0."""
    name = command.split()[0]
    return message_update(
        user,
        text=command,
        entities=[{"offset": 0, "length": len(name), "type": "bot_command"}],
    )


def _unique(prefix: str) -> str:
    return f"{prefix}{next(_file_seq):04d}"


def voice_update(user: TelegramUser, *, caption: str | None = None) -> dict[str, Any]:
    content: dict[str, Any] = {
        "voice": {
            "duration": 4,
            "mime_type": "audio/ogg",
            "file_id": _unique("AwACAgIAAxkBAAIBY2dVoiceNote-"),
            "file_unique_id": _unique("AgADvF8AAqk3yEo-"),
            "file_size": 15872,
        }
    }
    if caption is not None:
        content["caption"] = caption
    return message_update(user, **content)


def photo_sizes() -> list[dict[str, Any]]:
    """PhotoSizes as Telegram sends them: smallest first, the largest last."""
    n = next(_file_seq)
    return [
        {"file_id": f"AgACAgIAAxkBAAIBZWdPhoto{n:04d}-s90", "file_unique_id": f"AQADs{n}x", "file_size": 1324, "width": 90, "height": 68},
        {"file_id": f"AgACAgIAAxkBAAIBZWdPhoto{n:04d}-m320", "file_unique_id": f"AQADs{n}m", "file_size": 17853, "width": 320, "height": 240},
        {"file_id": f"AgACAgIAAxkBAAIBZWdPhoto{n:04d}-x800", "file_unique_id": f"AQADs{n}y", "file_size": 74129, "width": 800, "height": 600},
        {"file_id": f"AgACAgIAAxkBAAIBZWdPhoto{n:04d}-w1280", "file_unique_id": f"AQADs{n}w", "file_size": 141907, "width": 1280, "height": 960},
    ]


def photo_update(user: TelegramUser, sizes: list[dict[str, Any]], *, caption: str | None = None) -> dict[str, Any]:
    content: dict[str, Any] = {"photo": sizes}
    if caption is not None:
        content["caption"] = caption
    return message_update(user, **content)


def sticker_update(user: TelegramUser) -> dict[str, Any]:
    return message_update(
        user,
        sticker={
            "width": 512,
            "height": 512,
            "emoji": "👍",
            "set_name": "HotCherry",
            "is_animated": False,
            "is_video": False,
            "type": "regular",
            "thumbnail": {
                "file_id": _unique("AAMCAgADGQEAAIBZ2dStickerThumb-"),
                "file_unique_id": _unique("AQADthumb-"),
                "file_size": 5416,
                "width": 128,
                "height": 128,
            },
            "file_id": _unique("CAACAgIAAxkBAAIBZ2dSticker-"),
            "file_unique_id": _unique("AgADsticker-"),
            "file_size": 23456,
        },
    )


def document_update(user: TelegramUser, *, caption: str | None = None) -> dict[str, Any]:
    content: dict[str, Any] = {
        "document": {
            "file_name": "shopping-list.pdf",
            "mime_type": "application/pdf",
            "file_id": _unique("BQACAgIAAxkBAAIBaWdDocument-"),
            "file_unique_id": _unique("AgADdocument-"),
            "file_size": 48213,
        }
    }
    if caption is not None:
        content["caption"] = caption
    return message_update(user, **content)


def chat_id_of(update: dict[str, Any]) -> int:
    return update["message"]["chat"]["id"]


# --- Fake Telegram Bot API -----------------------------------------------------------

_API_PATH = re.compile(r"^/bot(?P<token>[^/]+)/(?P<method>[A-Za-z_]+)/?$")


@dataclass
class ApiCall:
    """One request the app made to the fake Bot API."""

    http_method: str
    path: str
    token: str | None
    method: str | None
    params: dict[str, Any]
    content_type: str
    received_at: float  # time.monotonic()
    status: int

    @property
    def is_send_message(self) -> bool:
        return self.token == BOT_TOKEN and (self.method or "").lower() == "sendmessage"

    def short(self) -> str:
        return f"{self.http_method} {self.path} -> {self.status} params={self.params!r}"


def _parse_multipart(content_type: str, body: bytes) -> dict[str, Any]:
    raw = f"Content-Type: {content_type}\r\n\r\n".encode() + body
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
    params: dict[str, Any] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        params[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return params


def _parse_params(content_type: str, body: bytes, query: str) -> dict[str, Any]:
    params: dict[str, Any] = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}
    media_type = content_type.split(";")[0].strip().lower()
    if not body:
        return params
    if media_type == "application/json":
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict):
            params.update(parsed)
    elif media_type == "application/x-www-form-urlencoded":
        form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        params.update({k: v[-1] for k, v in form.items()})
    elif media_type == "multipart/form-data":
        params.update(_parse_multipart(content_type, body))
    else:
        params["_raw_body"] = body.decode("utf-8", errors="replace")
    return params


class FakeBotApi:
    """A local stand-in for https://api.telegram.org that records every call.

    Answers `/bot<token>/<method>` with `{"ok": true, "result": ...}`: a Message for
    sendMessage, `true` for anything else. A wrong token gets Telegram's 401.
    """

    def __init__(self, token: str = BOT_TOKEN) -> None:
        self.token = token
        self._calls: list[ApiCall] = []
        self._cond = threading.Condition()
        self._next_message_id = itertools.count(50_001)
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                api._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                api._handle(self)

            def log_message(self, format: str, *args: Any) -> None:  # silence stderr
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-bot-api", daemon=True)

    # lifecycle

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeBotApi":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # request handling

    @staticmethod
    def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
        if "chunked" in handler.headers.get("Transfer-Encoding", "").lower():
            chunks = []
            while True:
                size = int(handler.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while handler.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(chunks)
                chunks.append(handler.rfile.read(size))
                handler.rfile.readline()
        length = int(handler.headers.get("Content-Length") or 0)
        return handler.rfile.read(length) if length else b""

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        received_at = time.monotonic()
        body = self._read_body(handler)
        split = urlsplit(handler.path)
        content_type = handler.headers.get("Content-Type", "")
        match = _API_PATH.match(split.path)
        token = match["token"] if match else None
        method = match["method"] if match else None
        try:
            params = _parse_params(content_type, body, split.query)
        except (ValueError, UnicodeDecodeError) as exc:
            params = {"_unparsable_body": body.decode("utf-8", errors="replace"), "_error": str(exc)}

        if match is None:
            status, payload = 404, {"ok": False, "error_code": 404, "description": "Not Found"}
        elif token != self.token:
            status, payload = 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        elif method.lower() == "sendmessage":
            status, payload = 200, {"ok": True, "result": self._message_result(params)}
        else:
            status, payload = 200, {"ok": True, "result": True}

        call = ApiCall(handler.command, handler.path, token, method, params, content_type, received_at, status)
        with self._cond:
            self._calls.append(call)
            self._cond.notify_all()

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _message_result(self, params: dict[str, Any]) -> dict[str, Any]:
        chat_id: Any = params.get("chat_id")
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            pass
        return {
            "message_id": next(self._next_message_id),
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "todobot", "username": "todobot_test_bot"},
            "chat": {"id": chat_id, "type": "private"},
            "date": int(time.time()),
            "text": params.get("text", ""),
        }

    # inspection

    @property
    def calls(self) -> list[ApiCall]:
        with self._cond:
            return list(self._calls)

    def mark(self) -> int:
        """A position in the call log; pass it as `since` to look only at later calls."""
        with self._cond:
            return len(self._calls)

    def send_messages(self, chat_id: int | None = None, since: int = 0) -> list[ApiCall]:
        return [
            c
            for c in self.calls[since:]
            if c.is_send_message and (chat_id is None or str(c.params.get("chat_id")) == str(chat_id))
        ]

    def wait_for_send_message(self, chat_id: int, *, since: int, deadline: float) -> ApiCall | None:
        """The first sendMessage to `chat_id` after `since`, or None at `deadline` (monotonic)."""
        with self._cond:
            while True:
                for call in self._calls[since:]:
                    if call.is_send_message and str(call.params.get("chat_id")) == str(chat_id):
                        return call
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)

    def describe_calls(self) -> str:
        calls = self.calls
        if not calls:
            return "  (the app made no Bot API calls)"
        return "\n".join(f"  {c.short()}" for c in calls)


# --- The app as a process ------------------------------------------------------------


def uv_executable() -> str:
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("`uv` is not on PATH; the contract starts the app with `uv run python -m todobot ...`", pytrace=False)
    return uv


def app_env(*, database_url: str, port: int, api_base: str) -> dict[str, str]:
    """The environment the contract defines, on top of the caller's (minus any contract vars)."""
    env = {k: v for k, v in os.environ.items() if k not in CONTRACT_ENV_VARS}
    env.update(
        DATABASE_URL=database_url,
        TELEGRAM_BOT_TOKEN=BOT_TOKEN,
        TELEGRAM_WEBHOOK_SECRET=WEBHOOK_SECRET,
        TELEGRAM_API_BASE=api_base,
        PORT=str(port),
        APP_VERSION=APP_VERSION,
        # Harness hygiene, not app behaviour: line-by-line stdout from a piped Python,
        # and no HTTP proxy between the app and the local fake Bot API.
        PYTHONUNBUFFERED="1",
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
    )
    return env


def run_migrate(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """`uv run python -m todobot migrate`; fails the test unless it exits 0."""
    command = [uv_executable(), "run", "python", "-m", "todobot", "migrate"]
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=MIGRATE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"the app could not migrate: `uv run python -m todobot migrate` did not finish in {MIGRATE_TIMEOUT:.0f} s\n"
            f"--- stdout ---\n{exc.stdout or ''}\n--- stderr ---\n{exc.stderr or ''}",
            pytrace=False,
        )
    if result.returncode != 0:
        pytest.fail(
            f"the app could not migrate: `uv run python -m todobot migrate` exited with code {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}--- stderr ---\n{result.stderr}",
            pytrace=False,
        )
    return result


class AppProcess:
    """`uv run python -m todobot serve` with its stdout captured line by line."""

    def __init__(self, env: dict[str, str], port: int, command: list[str] | None = None) -> None:
        self.env = env
        self.port = port
        self.command = command  # None: the contract's `uv run python -m todobot serve`
        self.base_url = f"http://127.0.0.1:{port}"
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._readers: list[threading.Thread] = []
        self._http = httpx.Client(base_url=self.base_url, timeout=30.0, trust_env=False)

    # lifecycle

    def start(self, timeout: float = APP_START_TIMEOUT) -> "AppProcess":
        command = self.command or [uv_executable(), "run", "python", "-m", "todobot", "serve"]
        self._proc = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        for stream, sink in ((self._proc.stdout, self._stdout), (self._proc.stderr, self._stderr)):
            reader = threading.Thread(target=self._pump, args=(stream, sink), daemon=True)
            reader.start()
            self._readers.append(reader)

        deadline = time.monotonic() + timeout
        while True:
            code = self._proc.poll()
            if code is not None:
                self._join_readers()
                pytest.fail(
                    f"the app could not start: `uv run python -m todobot serve` exited with code {code} "
                    f"before listening on 127.0.0.1:{self.port}\n{self.output()}",
                    pytrace=False,
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return self
            except OSError:
                pass
            if time.monotonic() >= deadline:
                self.stop()
                pytest.fail(
                    f"the app could not start: `uv run python -m todobot serve` did not accept connections on "
                    f"127.0.0.1:{self.port} within {timeout:.0f} s\n{self.output()}",
                    pytrace=False,
                )
            time.sleep(0.1)

    def _pump(self, stream: Any, sink: list[str]) -> None:
        for line in stream:
            with self._lock:
                sink.append(line.rstrip("\r\n"))
        stream.close()

    def _join_readers(self) -> None:
        for reader in self._readers:
            reader.join(timeout=5)

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> list[str]:
        """Stop the process (SIGTERM, then SIGKILL) and return everything it wrote to stdout."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)  # uv forwards it to the app
            try:
                proc.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=STOP_TIMEOUT)
        if proc is not None:
            self._join_readers()
        self._http.close()
        return self.stdout_lines

    # output

    @property
    def stdout_lines(self) -> list[str]:
        with self._lock:
            return list(self._stdout)

    @property
    def stderr_lines(self) -> list[str]:
        with self._lock:
            return list(self._stderr)

    @property
    def returncode(self) -> int | None:
        return None if self._proc is None else self._proc.poll()

    def output(self) -> str:
        stdout = "\n".join(self.stdout_lines) or "(nothing)"
        stderr = "\n".join(self.stderr_lines) or "(nothing)"
        return f"--- serve stdout ---\n{stdout}\n--- serve stderr ---\n{stderr}"

    # HTTP

    def post_update(self, update: dict[str, Any], *, secret: str = WEBHOOK_SECRET) -> httpx.Response:
        return self._http.post(WEBHOOK_PATH, json=update, headers={SECRET_HEADER: secret})

    def get(self, path: str) -> httpx.Response:
        return self._http.get(path)


# --- Sending an update and waiting for the reply ------------------------------------


@dataclass
class Delivery:
    update: dict[str, Any]
    response: httpx.Response
    reply: ApiCall
    elapsed: float  # seconds from just before the POST to the reply reaching the Bot API
    sent_at_wall: datetime  # wall clock just before the POST

    @property
    def reply_text(self) -> Any:
        return self.reply.params.get("text")


def deliver(app: AppProcess, api: FakeBotApi, update: dict[str, Any], *, within: float = DEFAULT_REPLY_TIMEOUT) -> Delivery:
    """POST `update` to the webhook, expect 200, and wait up to `within` s for a sendMessage to its chat."""
    chat_id = chat_id_of(update)
    since = api.mark()
    sent_at_wall = datetime.now().astimezone()
    t0 = time.monotonic()
    response = app.post_update(update)
    assert response.status_code == 200, (
        f"POST {WEBHOOK_PATH} answered {response.status_code}, the contract says 200; body: {response.text!r}\n"
        f"{app.output()}"
    )
    reply = api.wait_for_send_message(chat_id, since=since, deadline=t0 + within)
    if reply is None:
        pytest.fail(
            f"no sendMessage to chat_id={chat_id} reached the Bot API within {within:g} s of the update\n"
            f"Bot API calls recorded:\n{api.describe_calls()}\n{app.output()}",
            pytrace=False,
        )
    return Delivery(update, response, reply, reply.received_at - t0, sent_at_wall)


# --- Postgres ------------------------------------------------------------------------

INBOX_COLUMNS = ("user_id", "kind", "text", "file_id", "received_at")


def fetch_inbox(database_url: str) -> list[dict[str, Any]]:
    with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10) as conn:
        return conn.execute(
            "SELECT user_id, kind, text, file_id, received_at FROM inbox ORDER BY received_at, user_id"
        ).fetchall()


def wait_for_inbox_rows(database_url: str, count: int, *, timeout: float = 5.0) -> list[dict[str, Any]]:
    """The inbox rows once there are at least `count` of them (or whatever is there at the timeout)."""
    deadline = time.monotonic() + timeout
    while True:
        rows = fetch_inbox(database_url)
        if len(rows) >= count or time.monotonic() >= deadline:
            return rows
        time.sleep(0.1)


def stored(row: dict[str, Any]) -> dict[str, Any]:
    """The row without received_at, for exact comparison."""
    return {k: row[k] for k in ("user_id", "kind", "text", "file_id")}


# --- Logs ----------------------------------------------------------------------------


def json_records(lines: list[str]) -> list[dict[str, Any]]:
    """The stdout lines that are JSON objects, parsed; other lines are skipped."""
    records = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def log_line_violations(lines: list[str], version: str = APP_VERSION) -> list[str]:
    """Every stdout line must be one JSON object with level, event and version == `version`."""
    problems = []
    for number, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
        except ValueError:
            problems.append(f"line {number} is not JSON: {line!r}")
            continue
        if not isinstance(value, dict):
            problems.append(f"line {number} is JSON but not an object: {line!r}")
            continue
        missing = [key for key in ("level", "event", "version") if key not in value]
        if missing:
            problems.append(f"line {number} lacks {', '.join(missing)}: {line!r}")
        elif value["version"] != version:
            problems.append(f"line {number} has version {value['version']!r}, expected {version!r}: {line!r}")
    return problems


def find_records(lines: list[str], **expected: Any) -> list[dict[str, Any]]:
    return [r for r in json_records(lines) if all(r.get(k) == v for k, v in expected.items())]

