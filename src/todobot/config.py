"""Configuration from environment variables (the contract's "Environment" table)."""

from __future__ import annotations

import binascii
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from cryptography.fernet import Fernet

DEFAULT_API_BASE = "https://api.telegram.org"
DEFAULT_PORT = 8080
DEFAULT_VERSION = "dev"
DEFAULT_POSTHOG_HOST = "https://eu.i.posthog.com"
DEFAULT_TRELLO_API_BASE = "https://api.trello.com"
DEFAULT_TRELLO_AUTH_BASE = "https://trello.com"
DEFAULT_TRELLO_RETRY_SECONDS = 60.0  # TRL#3: "tries again once a minute"

ALLOWED_USER_IDS = "TELEGRAM_ALLOWED_USER_IDS"
_DIGITS = re.compile(r"[0-9]+")
# TRL's seams: Trello calls back to GET {PUBLIC_BASE_URL}/trello/callback.
TRELLO_CALLBACK_PATH = "/trello/callback"


class ConfigError(Exception):
    """A required variable is missing or a value is invalid."""


def app_version(env: Mapping[str, str] | None = None) -> str:
    """`APP_VERSION`, else Railway's `RAILWAY_GIT_COMMIT_SHA`, else `dev`; empty values count as unset."""
    env = os.environ if env is None else env
    return env.get("APP_VERSION") or env.get("RAILWAY_GIT_COMMIT_SHA") or DEFAULT_VERSION


def require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _port(env: Mapping[str, str]) -> int:
    raw = env.get("PORT") or str(DEFAULT_PORT)
    try:
        port = int(raw)
    except ValueError:
        raise ConfigError(f"PORT must be an integer, got {raw!r}") from None
    if not 0 <= port <= 65535:
        raise ConfigError(f"PORT must be between 0 and 65535, got {port}")
    return port


def allowed_user_ids(env: Mapping[str, str]) -> frozenset[int] | None:
    """ACC: the allow list, the Telegram user ids `serve` serves; None, every user, when it is unset or empty.

    The ids are separated by commas, with spaces allowed around them. Anything else is a ConfigError. Its message says
    which item is wrong, never what it holds: `config_error` also goes to PostHog, which gets no user id (ERR).
    """
    raw = env.get(ALLOWED_USER_IDS, "")
    if not raw.strip():
        return None
    items = raw.split(",")
    ids: set[int] = set()
    for number, item in enumerate(items, 1):
        value = item.strip()
        if not _DIGITS.fullmatch(value) or int(value) == 0:
            raise ConfigError(
                f"{ALLOWED_USER_IDS} must be Telegram user ids separated by commas; "
                f"item {number} of {len(items)} is not one"
            )
        ids.add(int(value))
    return frozenset(ids)


def _base_url(env: Mapping[str, str], name: str, default: str | None = None) -> str:
    """An http(s) base URL without its trailing slash; required when there is no default."""
    raw = (env.get(name) or default or "").strip()
    if not raw:
        raise ConfigError(f"{name} is required")
    if not raw.startswith(("https://", "http://")):
        raise ConfigError(f"{name} must be an http:// or https:// URL")
    return raw.rstrip("/")


def _retry_seconds(env: Mapping[str, str]) -> float:
    raw = env.get("TRELLO_RETRY_SECONDS") or str(DEFAULT_TRELLO_RETRY_SECONDS)
    try:
        seconds = float(raw)
    except ValueError:
        raise ConfigError(f"TRELLO_RETRY_SECONDS must be a number of seconds, got {raw!r}") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ConfigError(f"TRELLO_RETRY_SECONDS must be more than 0, got {raw!r}")
    return seconds


def _secret_key(env: Mapping[str, str]) -> str:
    """APP_SECRET_KEY, checked to be a Fernet key. The message never quotes it: config_error goes to PostHog."""
    key = require(env, "APP_SECRET_KEY").strip()
    try:
        Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError, binascii.Error):
        raise ConfigError(
            "APP_SECRET_KEY must be a Fernet key: 32 random bytes in url-safe base64 (44 characters), e.g. from "
            '`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`'
        ) from None
    return key


@dataclass(frozen=True, kw_only=True)
class TrelloSettings:
    """TRL: the bot's Trello app and where Trello and the bot are. The secrets stay out of repr."""

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    public_base_url: str  # the bot's own address, for Trello's link back (the OAuth callback)
    secret_key: str = field(repr=False)  # APP_SECRET_KEY: encrypts the users' Trello tokens in the database
    api_base: str = DEFAULT_TRELLO_API_BASE
    auth_base: str = DEFAULT_TRELLO_AUTH_BASE
    retry_seconds: float = DEFAULT_TRELLO_RETRY_SECONDS

    @property
    def callback_url(self) -> str:
        return f"{self.public_base_url}{TRELLO_CALLBACK_PATH}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TrelloSettings | None:
        """None without `TRELLO_API_KEY` (or with an empty one): then the bot has no Trello at all (TRL's seams).
        With it, the rest of Trello's variables are required, and a bad value is a ConfigError."""
        env = os.environ if env is None else env
        api_key = env.get("TRELLO_API_KEY", "").strip()
        if not api_key:
            return None
        return cls(
            api_key=api_key,
            api_secret=require(env, "TRELLO_API_SECRET").strip(),
            public_base_url=_base_url(env, "PUBLIC_BASE_URL"),
            secret_key=_secret_key(env),
            api_base=_base_url(env, "TRELLO_API_BASE", DEFAULT_TRELLO_API_BASE),
            auth_base=_base_url(env, "TRELLO_AUTH_BASE", DEFAULT_TRELLO_AUTH_BASE),
            retry_seconds=_retry_seconds(env),
        )


@dataclass(frozen=True, kw_only=True)
class Settings:
    """What `serve` needs. Secrets and user ids stay out of repr, so a logged Settings leaks nothing."""

    database_url: str = field(repr=False)
    bot_token: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    api_base: str = DEFAULT_API_BASE
    port: int = DEFAULT_PORT
    version: str = DEFAULT_VERSION
    # ACC: the only users the bot serves; None serves every user, as before.
    allowed_user_ids: frozenset[int] | None = field(default=None, repr=False)
    # TRL: None without TRELLO_API_KEY, and then there is no Trello at all.
    trello: TrelloSettings | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            database_url=require(env, "DATABASE_URL"),
            bot_token=require(env, "TELEGRAM_BOT_TOKEN"),
            # An empty secret would switch the webhook check off, so it is required like the token.
            webhook_secret=require(env, "TELEGRAM_WEBHOOK_SECRET"),
            api_base=env.get("TELEGRAM_API_BASE") or DEFAULT_API_BASE,
            port=_port(env),
            version=app_version(env),
            allowed_user_ids=allowed_user_ids(env),
            trello=TrelloSettings.from_env(env),
        )


@dataclass(frozen=True, kw_only=True)
class PostHogSettings:
    """Where `serve` sends its errors (ERR): PostHog's project token and ingestion host. The token stays out of repr."""

    token: str = field(repr=False)
    host: str = DEFAULT_POSTHOG_HOST

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PostHogSettings | None:
        """None without `POSTHOG_PROJECT_TOKEN` (or with an empty one): then `serve` sends PostHog nothing."""
        env = os.environ if env is None else env
        token = env.get("POSTHOG_PROJECT_TOKEN", "").strip()
        if not token:
            return None
        return cls(token=token, host=env.get("POSTHOG_HOST") or DEFAULT_POSTHOG_HOST)
