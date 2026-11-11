"""Configuration from environment variables (the contract's "Environment" table)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_API_BASE = "https://api.telegram.org"
DEFAULT_PORT = 8080
DEFAULT_VERSION = "dev"
DEFAULT_POSTHOG_HOST = "https://eu.i.posthog.com"

ALLOWED_USER_IDS = "TELEGRAM_ALLOWED_USER_IDS"
_DIGITS = re.compile(r"[0-9]+")


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
