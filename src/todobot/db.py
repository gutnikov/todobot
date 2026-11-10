"""The database engine: SQLAlchemy (async) over asyncpg, created from the contract's `DATABASE_URL`."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DRIVER = "postgresql+asyncpg"
_ACCEPTED_SCHEMES = frozenset({"postgresql", "postgres", DRIVER})

# libpq options asyncpg has no keyword for. asyncpg cannot do channel binding; Neon's console adds
# `channel_binding=require` to its URLs, and the connection is still TLS through `sslmode`.
_DROPPED_OPTIONS = frozenset({"channel_binding"})

# How long opening one connection may take. Generous enough for a Neon compute that is waking up.
CONNECT_TIMEOUT_SECONDS = 10.0


def _last(value: str | Sequence[str]) -> str:
    return value if isinstance(value, str) else value[-1]


def to_asyncpg_url(database_url: str) -> URL:
    """`postgresql://user:pass@host:port/db?sslmode=require` as the URL SQLAlchemy's asyncpg dialect accepts.

    The dialect passes query options to `asyncpg.connect()` as keywords, and asyncpg names libpq's `sslmode`
    `ssl` (with the same values: disable, allow, prefer, require, verify-ca, verify-full).
    """
    try:
        url = make_url(database_url)
    except Exception:  # SQLAlchemy's ArgumentError quotes the URL, password included: do not chain it
        raise ValueError("DATABASE_URL is not a valid database URL") from None
    if url.drivername not in _ACCEPTED_SCHEMES:
        raise ValueError(f"DATABASE_URL must be a postgresql:// URL, got the scheme {url.drivername!r}")

    query: dict[str, str | Sequence[str]] = {
        key: value for key, value in url.query.items() if key not in _DROPPED_OPTIONS
    }
    sslmode = query.pop("sslmode", None)
    if sslmode is not None:
        query["ssl"] = _last(sslmode)
    return url.set(drivername=DRIVER, query=query)


def create_engine(database_url: str) -> AsyncEngine:
    """An engine that connects lazily: creating it never touches the database."""
    return create_async_engine(
        to_asyncpg_url(database_url),
        # Neon closes idle connections when its compute suspends; check a pooled one before using it.
        pool_pre_ping=True,
        connect_args={"timeout": CONNECT_TIMEOUT_SECONDS},
    )
