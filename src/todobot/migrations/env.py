"""Alembic's environment.

`python -m todobot migrate` hands over its connection in `config.attributes["connection"]`. The alembic CLI
(`uv run alembic revision --autogenerate -m "..."`, configured in pyproject.toml) has none, so this connects to
`DATABASE_URL` itself.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection

from todobot.db import create_engine
from todobot.schema import metadata

config = context.config


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_from_env() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to run alembic")
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline (--sql) migrations are not supported; run them against a database")

shared_connection = config.attributes.get("connection")
if shared_connection is not None:
    run_migrations(shared_connection)
else:
    asyncio.run(run_migrations_from_env())
