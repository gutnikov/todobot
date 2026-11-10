"""DATABASE_URL (`postgresql://…`, Neon's with `?sslmode=require`) into what SQLAlchemy's asyncpg dialect accepts."""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from sqlalchemy import text

from todobot.db import create_engine, to_asyncpg_url


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        pytest.param("postgresql://u:p@db.local:5432/app", "postgresql+asyncpg://u:p@db.local:5432/app", id="plain"),
        pytest.param("postgres://u:p@db.local/app", "postgresql+asyncpg://u:p@db.local/app", id="postgres-scheme"),
        pytest.param(
            "postgresql://u:p@ep-cool-1.eu-central-1.aws.neon.tech/neondb?sslmode=require",
            "postgresql+asyncpg://u:p@ep-cool-1.eu-central-1.aws.neon.tech/neondb?ssl=require",
            id="neon",
        ),
        pytest.param(
            "postgresql://u:p@ep-cool-1.neon.tech/neondb?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@ep-cool-1.neon.tech/neondb?ssl=require",
            id="neon-console-with-channel-binding",
        ),
        pytest.param(
            "postgresql://u:p@db.local/app?sslmode=verify-full&application_name=todobot",
            "postgresql+asyncpg://u:p@db.local/app?application_name=todobot&ssl=verify-full",
            id="other-options-kept",
        ),
        pytest.param(
            "postgresql+asyncpg://u:p@db.local/app?ssl=require",
            "postgresql+asyncpg://u:p@db.local/app?ssl=require",
            id="already-asyncpg",
        ),
    ],
)
def test_to_asyncpg_url(given: str, expected: str) -> None:
    assert to_asyncpg_url(given).render_as_string(hide_password=False) == expected


def test_password_with_escaped_characters_survives() -> None:
    url = to_asyncpg_url("postgresql://neondb_owner:p%40ss%2Fw%3Ard@host/neondb?sslmode=require")
    assert url.password == "p@ss/w:rd"
    assert url.username == "neondb_owner"


@pytest.mark.parametrize("given", ["mysql://u:p@db.local/app", "sqlite:///app.db", "not a url"])
def test_rejects_what_is_not_postgres(given: str) -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        to_asyncpg_url(given)


def test_error_does_not_leak_the_password() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL") as raised:
        to_asyncpg_url("::secret-password@nowhere")
    assert "secret-password" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


# Against a real server: the converted URL's options are ones asyncpg accepts.


async def _select_one(database_url: str) -> int:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as conn:
            return int((await conn.execute(text("SELECT 1"))).scalar_one())
    finally:
        await engine.dispose()


@pytest.mark.parametrize("options", ["", "?sslmode=disable", "?sslmode=disable&channel_binding=require"])
def test_converted_url_connects(database_url: str, options: str) -> None:
    assert asyncio.run(_select_one(database_url + options)) == 1


def test_sslmode_require_reaches_asyncpg(database_url: str) -> None:
    """With `sslmode=require`, the connection is TLS or none at all."""
    with psycopg.connect(database_url) as conn:
        server_has_tls = conn.execute("SHOW ssl").fetchone() == ("on",)
    if server_has_tls:
        assert asyncio.run(_select_one(database_url + "?sslmode=require")) == 1
    else:
        # The embedded test server has no TLS, so asyncpg, told to require it, refuses to connect.
        with pytest.raises(Exception, match=r"(?i)ssl"):
            asyncio.run(_select_one(database_url + "?sslmode=require"))
