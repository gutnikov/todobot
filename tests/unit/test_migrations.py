"""`migrate` creates the schema the code describes, and running it again changes nothing."""

from __future__ import annotations

import asyncio
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy.engine import Connection

from todobot import migrate
from todobot.db import create_engine
from todobot.schema import metadata


def _diff(connection: Connection) -> list[Any]:
    return list(compare_metadata(MigrationContext.configure(connection), metadata))


async def _schema_diff(database_url: str) -> list[Any]:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_diff)
    finally:
        await engine.dispose()


def test_migrations_match_the_schema_in_code(migrated_database_url: str) -> None:
    assert asyncio.run(_schema_diff(migrated_database_url)) == []


def test_migrate_twice_is_a_no_op(migrated_database_url: str) -> None:
    asyncio.run(migrate.upgrade(migrated_database_url))
    assert asyncio.run(_schema_diff(migrated_database_url)) == []
