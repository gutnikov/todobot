"""Settings from the environment, the version fallback, and the JSON log lines."""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys

import pytest

from todobot.config import ConfigError, Settings, app_version
from todobot.logs import JsonFormatter, log_event

REQUIRED = {
    "DATABASE_URL": "postgresql://u:p@db.local/app",
    "TELEGRAM_BOT_TOKEN": "123:token",
    "TELEGRAM_WEBHOOK_SECRET": "s3cret",
}


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"APP_VERSION": "1.2.3", "RAILWAY_GIT_COMMIT_SHA": "abc123"}, "1.2.3"),
        ({"RAILWAY_GIT_COMMIT_SHA": "abc123"}, "abc123"),
        ({"APP_VERSION": "", "RAILWAY_GIT_COMMIT_SHA": "abc123"}, "abc123"),
        ({}, "dev"),
    ],
)
def test_version_falls_back_to_the_railway_commit_then_dev(env: dict[str, str], expected: str) -> None:
    assert app_version(env) == expected


def test_settings_defaults() -> None:
    settings = Settings.from_env(REQUIRED)
    assert settings.api_base == "https://api.telegram.org"
    assert settings.port == 8080
    assert settings.version == "dev"


@pytest.mark.parametrize("name", sorted(REQUIRED))
@pytest.mark.parametrize("value", [None, ""])
def test_settings_require(name: str, value: str | None) -> None:
    env = {k: v for k, v in REQUIRED.items() if k != name}
    if value is not None:
        env[name] = value
    with pytest.raises(ConfigError, match=name):
        Settings.from_env(env)


@pytest.mark.parametrize("port", ["http", "-1", "70000"])
def test_settings_reject_a_bad_port(port: str) -> None:
    with pytest.raises(ConfigError, match="PORT"):
        Settings.from_env({**REQUIRED, "PORT": port})


def test_settings_repr_hides_secrets() -> None:
    shown = repr(Settings.from_env(REQUIRED))
    assert "token" not in shown
    assert "s3cret" not in shown
    assert "u:p@" not in shown


def _format(emit: object) -> dict[str, object]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("v-test"))
    logger = logging.getLogger(f"todobot.test.{id(stream)}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    assert callable(emit)
    emit(logger)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1, lines
    record = json.loads(lines[0])
    assert isinstance(record, dict)
    return record


def test_own_event_line() -> None:
    record = _format(lambda logger: log_event(logger, "inbox_saved", kind="photo", user_id=7))
    assert record["level"] == "INFO"
    assert record["event"] == "inbox_saved"
    assert record["version"] == "v-test"
    assert record["kind"] == "photo"
    assert record["user_id"] == 7
    assert "message" not in record


def test_library_line_gets_a_generic_event_and_keeps_its_message() -> None:
    record = _format(lambda logger: logger.warning("Update id=%s is %s", 1, "handled"))
    assert record["level"] == "WARNING"
    assert record["event"] == "log"
    assert record["version"] == "v-test"
    assert record["message"] == "Update id=1 is handled"


def test_traceback_stays_inside_the_one_line() -> None:
    def emit(logger: logging.Logger) -> None:
        try:
            1 / 0  # noqa: B018
        except ZeroDivisionError as exc:
            log_event(logger, "handler_error", level=logging.ERROR, exc_info=exc)

    record = _format(emit)
    assert record["level"] == "ERROR"
    assert record["event"] == "handler_error"
    assert "ZeroDivisionError" in str(record["exc"])


def test_extra_fields_cannot_replace_the_version_or_level() -> None:
    record = _format(lambda logger: logger.info("x", extra={"version": "forged", "level": "DEBUG"}))
    assert record["version"] == "v-test"
    assert record["level"] == "INFO"


def test_setup_logging_turns_warnings_and_crashes_into_json_lines_on_stdout() -> None:
    script = (
        "import logging, warnings\n"
        "from todobot.logs import setup_logging\n"
        "setup_logging('v-sub')\n"
        "logging.getLogger('some.library').info('plain %s', 'text')\n"
        "warnings.warn('careful')\n"
        "raise RuntimeError('boom')\n"
    )
    result = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )

    assert result.returncode == 1
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert all({"level", "event", "version"} <= record.keys() for record in records), result.stdout
    assert {record["version"] for record in records} == {"v-sub"}
    assert [record["event"] for record in records] == ["log", "log", "uncaught_exception"]
    assert "RuntimeError: boom" in records[-1]["exc"]
