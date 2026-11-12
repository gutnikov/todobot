"""TRL's environment: no Trello at all without TRELLO_API_KEY; with it, the rest is required and checked at start, and
no secret shows in a repr or an error."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from todobot.config import ConfigError, Settings, TrelloSettings

REQUIRED = {
    "DATABASE_URL": "postgresql://u:p@db.local/app",
    "TELEGRAM_BOT_TOKEN": "123:token",
    "TELEGRAM_WEBHOOK_SECRET": "s3cret",
}
SECRET_KEY = Fernet.generate_key().decode()
TRELLO = {
    "TRELLO_API_KEY": "trello-key-1234",
    "TRELLO_API_SECRET": "trello-secret-5678",
    "PUBLIC_BASE_URL": "https://todobot.example/",
    "APP_SECRET_KEY": SECRET_KEY,
}


@pytest.mark.parametrize("key", [None, "", "  "])
def test_without_a_trello_key_there_is_no_trello(key: str | None) -> None:
    env = {**REQUIRED, **{name: value for name, value in TRELLO.items() if name != "TRELLO_API_KEY"}}
    if key is not None:
        env["TRELLO_API_KEY"] = key

    assert Settings.from_env(env).trello is None


def test_trello_settings_and_their_defaults() -> None:
    trello = Settings.from_env({**REQUIRED, **TRELLO}).trello

    assert trello == TrelloSettings(
        api_key="trello-key-1234",
        api_secret="trello-secret-5678",
        public_base_url="https://todobot.example",
        secret_key=SECRET_KEY,
        api_base="https://api.trello.com",
        auth_base="https://trello.com",
        retry_seconds=60.0,
    )
    assert trello.callback_url == "https://todobot.example/trello/callback"


def test_trello_settings_from_the_environment() -> None:
    env = {
        **TRELLO,
        "TRELLO_API_BASE": "http://127.0.0.1:9001/",
        "TRELLO_AUTH_BASE": "http://127.0.0.1:9002",
        "TRELLO_RETRY_SECONDS": "1.5",
    }

    trello = TrelloSettings.from_env(env)

    assert trello is not None
    assert (trello.api_base, trello.auth_base, trello.retry_seconds) == (
        "http://127.0.0.1:9001",
        "http://127.0.0.1:9002",
        1.5,
    )


@pytest.mark.parametrize("name", ["TRELLO_API_SECRET", "PUBLIC_BASE_URL", "APP_SECRET_KEY"])
@pytest.mark.parametrize("value", [None, ""])
def test_with_a_trello_key_the_rest_is_required(name: str, value: str | None) -> None:
    env = {k: v for k, v in TRELLO.items() if k != name}
    if value is not None:
        env[name] = value

    with pytest.raises(ConfigError, match=name):
        TrelloSettings.from_env(env)


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("too-short", id="not-base64-of-32-bytes"),
        pytest.param("a" * 32, id="32-characters-not-32-bytes"),
        pytest.param("ключ" * 11, id="not-ascii"),
    ],
)
def test_an_app_secret_key_that_is_not_a_fernet_key_is_refused_without_quoting_it(key: str) -> None:
    with pytest.raises(ConfigError, match="APP_SECRET_KEY must be a Fernet key") as raised:
        TrelloSettings.from_env({**TRELLO, "APP_SECRET_KEY": key})

    assert key not in str(raised.value)


@pytest.mark.parametrize("seconds", ["0", "-1", "soon", "nan", "inf"])
def test_retry_seconds_must_be_a_positive_number(seconds: str) -> None:
    with pytest.raises(ConfigError, match="TRELLO_RETRY_SECONDS"):
        TrelloSettings.from_env({**TRELLO, "TRELLO_RETRY_SECONDS": seconds})


@pytest.mark.parametrize("name", ["PUBLIC_BASE_URL", "TRELLO_API_BASE", "TRELLO_AUTH_BASE"])
def test_addresses_must_be_http_urls(name: str) -> None:
    with pytest.raises(ConfigError, match=name):
        TrelloSettings.from_env({**TRELLO, name: "todobot.example"})


def test_the_repr_shows_no_secret() -> None:
    shown = repr(Settings.from_env({**REQUIRED, **TRELLO}))

    for secret in ("trello-key-1234", "trello-secret-5678", SECRET_KEY):
        assert secret not in shown
    assert "https://todobot.example" in shown
