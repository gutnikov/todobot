"""The engine as src/todobot/db.py makes it: errors without the statement's parameters."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)
