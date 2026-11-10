"""`python -m todobot migrate`: Alembic, run programmatically on the app's own engine."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection

from todobot.db import create_engine

SCRIPT_LOCATION = "todobot:migrations"


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    return config


def _upgrade(connection: Connection, revision: str) -> None:
    config = alembic_config()
    # env.py runs the migrations on this connection, inside the transaction `upgrade` opened.
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


async def upgrade(database_url: str, revision: str = "head") -> None:
    """Apply the migrations up to `revision`, all in one transaction."""
    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade, revision)
    finally:
        await engine.dispose()
