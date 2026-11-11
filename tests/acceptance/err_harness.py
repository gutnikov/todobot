"""What the ERR acceptance tests need beyond SK's and DUP's harness (agentify/specs/requirements/ERR.yaml, `seams`).

- A fake PostHog at POSTHOG_HOST: an HTTP server that records every request, whatever its path and method, and
  answers 200 `{"status": 1}` (PostHog takes); the refusing one answers every request with 500 (PostHog refuses).
- `serve` started with POSTHOG_PROJECT_TOKEN and POSTHOG_HOST pointing at the fake: the state
  runningBotWithErrorTracking.
- The events read from the recorded bodies: a body is one event or a batch (`batch: [...]`), each event with
  `event`, `distinct_id` and `properties`. Bodies may come gzipped, or form-encoded with the JSON in a `data` field.

Black-box like harness.py: nothing here imports the app or PostHog's SDK.
"""

from __future__ import annotations

import base64
import gzip
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote_plus

from harness import AppProcess, FakeBotApi, app_env, find_records, free_port, json_records, run_migrate

POSTHOG_TOKEN = "phc_test_token"  # fake: the project token PostHog's SDK sends with every request
POSTHOG_WAIT = 10.0  # seconds: the seam lets the process take up to 10 s to send what it has queued
ERROR_LEVELS = frozenset({"ERROR", "CRITICAL"})
DESCRIBE_LIMIT = 1500  # characters of each event's properties shown in a failure message

_GZIP_MAGIC = b"\x1f\x8b"


# --- Reading what PostHog got -------------------------------------------------------


def _gunzip(data: bytes) -> bytes:
    """`data` decompressed when it is gzip (`Content-Encoding: gzip`, or PostHog's `compression=gzip…`)."""
    if data[:2] != _GZIP_MAGIC:
        return data
    try:
        return gzip.decompress(data)
    except (OSError, EOFError):
        return data


def _base64(text: str) -> bytes | None:
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        return None


def _json_values(data: bytes) -> list[Any]:
    """The JSON in a body: the body itself, or a form's `data` field (plain JSON or base64, maybe gzipped)."""
    text = data.decode("utf-8", errors="replace")
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    values = []
    for field in parse_qs(text, keep_blank_values=True).get("data", []):
        for candidate in (field.encode("utf-8"), _base64(field)):
            if candidate is None:
                continue
            try:
                values.append(json.loads(_gunzip(candidate).decode("utf-8")))
                break
            except (ValueError, UnicodeDecodeError):
                continue
    return values


def events_in(value: Any) -> Iterator[dict[str, Any]]:
    """The events in a JSON value PostHog got: one event, a batch (`batch: [...]`), or a list of either."""
    if isinstance(value, list):
        for item in value:
            yield from events_in(item)
    elif isinstance(value, dict):
        batch = value.get("batch")
        if isinstance(batch, list):
            yield from events_in(batch)
        elif "event" in value:
            yield value


def values_in(value: Any) -> Iterator[Any]:
    """Every scalar inside a JSON value, however deep it sits."""
    if isinstance(value, dict):
        for item in value.values():
            yield from values_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from values_in(item)
    else:
        yield value


def carries(properties: Any, wanted: str) -> bool:
    """Whether `wanted` is the value of one of the properties (any name, at any depth)."""
    return any(value == wanted for value in values_in(properties))


@dataclass
class PostHogRequest:
    """One request the app made to the fake PostHog."""

    http_method: str
    path: str
    headers: dict[str, str]
    body: bytes  # as it arrived
    decoded: bytes  # gunzipped when it arrived gzipped
    payloads: list[Any]  # the JSON values found in the body
    status: int

    @property
    def events(self) -> list[dict[str, Any]]:
        return [event for payload in self.payloads for event in events_in(payload)]

    def searchable_text(self) -> str:
        """Everything this request carried, decoded: path, headers, the body as text, URL-decoded, and its JSON
        with non-ASCII letters as letters (an SDK may send «молоко» as `\\u043c…`)."""
        text = self.decoded.decode("utf-8", errors="replace")
        parts = [unquote_plus(self.path), *(f"{name}: {value}" for name, value in self.headers.items())]
        parts += [text, unquote_plus(text)]
        parts += [json.dumps(payload, ensure_ascii=False) for payload in self.payloads]
        return "\n".join(parts)


class FakePostHog:
    """A local stand-in for PostHog's ingestion API that records every request.

    Takes everything (200, `{"status": 1}`), or with `refuse=True` answers every request with 500.
    """

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self._requests: list[PostHogRequest] = []
        self._cond = threading.Condition()
        posthog = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                posthog._handle(self)

            def do_GET(self) -> None:
                posthog._handle(self)

            def do_PUT(self) -> None:
                posthog._handle(self)

            def do_PATCH(self) -> None:
                posthog._handle(self)

            def do_DELETE(self) -> None:
                posthog._handle(self)

            def log_message(self, *args: Any) -> None:  # silence stderr
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-posthog", daemon=True)

    # lifecycle

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakePostHog:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # request handling

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        body = FakeBotApi._read_body(handler)  # chunked or Content-Length, as SK's fake Bot API reads it
        decoded = _gunzip(body)
        payloads = _json_values(decoded)
        if self.refuse:
            status, answer = 500, {"status": 0, "error": "the test's PostHog refuses everything"}
        else:
            status, answer = 200, {"status": 1}
        request = PostHogRequest(
            handler.command, handler.path, dict(handler.headers.items()), body, decoded, payloads, status
        )
        with self._cond:
            self._requests.append(request)
            self._cond.notify_all()

        data = json.dumps(answer).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    # inspection

    @property
    def requests(self) -> list[PostHogRequest]:
        with self._cond:
            return list(self._requests)

    @property
    def events(self) -> list[dict[str, Any]]:
        return [event for request in self.requests for event in request.events]

    def events_named(self, name: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("event") == name]

    def wait_for_event(self, name: str, *, within: float = POSTHOG_WAIT) -> bool:
        """Whether an event `name` arrived within `within` s (it returns as soon as one has)."""
        deadline = time.monotonic() + within
        with self._cond:
            while True:
                if any(event.get("event") == name for request in self._requests for event in request.events):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)

    def received_any_of(self, needles: list[str]) -> list[str]:
        """The `needles` found (case-insensitively) anywhere in what PostHog got, each with where it was found."""
        found = []
        for number, request in enumerate(self.requests, 1):
            text = request.searchable_text().casefold()
            found += [
                f"{needle!r} in request #{number} ({request.http_method} {request.path})"
                for needle in needles
                if needle.casefold() in text
            ]
        return found

    def describe(self) -> str:
        requests = self.requests
        if not requests:
            return "  (PostHog got no requests)"
        out = []
        for request in requests:
            out.append(f"  {request.http_method} {request.path} -> {request.status}")
            for event in request.events:
                properties = json.dumps(event.get("properties"), ensure_ascii=False)
                if len(properties) > DESCRIBE_LIMIT:
                    properties = properties[:DESCRIBE_LIMIT] + "…"
                out.append(
                    f"    event={event.get('event')!r} distinct_id={event.get('distinct_id')!r} properties={properties}"
                )
            if not request.events:
                out.append(f"    (no events) body={request.decoded[:DESCRIBE_LIMIT]!r}")
        return "\n".join(out)


# --- The app with error tracking ----------------------------------------------------


@contextmanager
def serve_with_posthog(
    database_url: str, fake_api: FakeBotApi, posthog: FakePostHog, *, migrate: bool = True
) -> Iterator[AppProcess]:
    """`serve` in the state runningBotWithErrorTracking: SK's environment plus POSTHOG_PROJECT_TOKEN and
    POSTHOG_HOST (the fake). `migrate` (unless migrate=False: Database storingFails) runs first with SK's environment
    only, as in every SK and DUP example. The app is stopped on the way out."""
    port = free_port()
    env = app_env(database_url=database_url, port=port, api_base=fake_api.base_url)
    if migrate:
        run_migrate(env)
    env.update(POSTHOG_PROJECT_TOKEN=POSTHOG_TOKEN, POSTHOG_HOST=posthog.base_url)
    app = AppProcess(env, port)
    try:
        yield app.start()
    finally:
        app.stop()


def wait_for_log_line(app: AppProcess, *, within: float = POSTHOG_WAIT, **expected: Any) -> bool:
    """Whether a stdout line with the `expected` fields showed up within `within` s (returns as soon as one has)."""
    deadline = time.monotonic() + within
    while True:
        if find_records(app.stdout_lines, **expected):
            return True
        if time.monotonic() >= deadline or not app.is_running():
            return False
        time.sleep(0.1)


def error_lines(lines: list[str]) -> list[dict[str, Any]]:
    """The stdout lines with level ERROR or CRITICAL."""
    return [record for record in json_records(lines) if str(record.get("level", "")).upper() in ERROR_LEVELS]


def describe_failure(app: AppProcess, posthog: FakePostHog) -> str:
    return f"What PostHog got:\n{posthog.describe()}\n{app.output()}"
