"""Fixtures for the unit tests: a real Postgres (TEST_DATABASE_URL, or an embedded one from pgserver)."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from todobot import migrate


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def postgres_server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A server where tests may create databases."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return

    import pgserver  # only needed without TEST_DATABASE_URL

    pgdata = tmp_path_factory.mktemp("postgres") / "pgdata"
    pgserver.initdb(
        ["--auth=trust", "--auth-local=trust", "--encoding=utf8", "--no-instructions", "-U", "postgres"],
        pgdata=pgdata,
        timeout=120,
    )
    port = _free_port()
    log = Path(pgdata.parent) / "postgres.log"
    pgserver.pg_ctl(
        ["-w", "-t", "60", "-o", f"-h 127.0.0.1 -p {port} -k ''", "-l", str(log), "start"], pgdata=pgdata, timeout=90
    )
    try:
        yield f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
    finally:
        pgserver.pg_ctl(["-w", "-m", "fast", "stop"], pgdata=pgdata, timeout=60)


@pytest.fixture
def database_url(postgres_server_url: str) -> Iterator[str]:
    """A fresh, empty database for this test only."""
    name = f"todobot_unit_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_server_url, autocommit=True, connect_timeout=10) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parts = urlsplit(postgres_server_url)
    try:
        yield urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))
    finally:
        with psycopg.connect(postgres_server_url, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def migrated_database_url(database_url: str) -> str:
    asyncio.run(migrate.upgrade(database_url))
    return database_url
