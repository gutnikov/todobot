"""`migrate` creates the schema the code describes, and running it again changes nothing."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
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


# The INSERT the version before DUP makes: no message_id.
OLD_INSERT = "INSERT INTO inbox (user_id, kind, text, file_id) VALUES (%s, 'text', %s, NULL)"


def test_0002_keeps_the_rows_before_it_and_the_previous_versions_inserts(database_url: str) -> None:
    """Railway migrates while the previous version still serves: its rows stay, and its INSERTs keep working."""
    asyncio.run(migrate.upgrade(database_url, "0001"))
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO users (id) VALUES (7)")
        conn.execute(OLD_INSERT, (7, "до"))

    asyncio.run(migrate.upgrade(database_url))

    with psycopg.connect(database_url, autocommit=True) as conn:
        # Rows without a message_id never conflict with each other: Postgres counts NULLs as distinct.
        conn.execute(OLD_INSERT, (7, "после"))
        conn.execute(OLD_INSERT, (7, "после"))
        rows = conn.execute("SELECT text, message_id FROM inbox ORDER BY received_at").fetchall()
    assert rows == [("до", None), ("после", None), ("после", None)]


# The INSERT the version before CAP makes (DUP's): one inbox item per message, and no task.
DUP_INSERT = (
    "INSERT INTO inbox (user_id, message_id, kind, text, file_id) VALUES (%s, %s, 'text', %s, NULL) "
    "ON CONFLICT (user_id, message_id) DO NOTHING"
)


def test_0003_makes_no_task_of_earlier_items_and_keeps_the_previous_versions_inserts(database_url: str) -> None:
    """CAP#1: inbox items stored before it do not become tasks. Railway migrates while the previous version still
    serves: its INSERTs keep working, and make no task either."""
    asyncio.run(migrate.upgrade(database_url, "0002"))
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO users (id) VALUES (7)")
        conn.execute(DUP_INSERT, (7, 1, "до"))

    asyncio.run(migrate.upgrade(database_url))

    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(DUP_INSERT, (7, 2, "после"))
        conn.execute(DUP_INSERT, (7, 2, "после"))
        items = conn.execute("SELECT message_id, text FROM inbox ORDER BY message_id").fetchall()
        tasks = conn.execute("SELECT count(*) FROM tasks").fetchone()
    assert items == [(1, "до"), (2, "после")]
    assert tasks == (0,)
