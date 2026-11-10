"""Fixtures for the black-box acceptance tests (contract: agentify/specs/requirements/skeleton.md, "Test seams")."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from harness import (
    AppProcess,
    FakeBotApi,
    app_env,
    free_port,
    run_migrate,
    with_database,
)

_APPS = pytest.StashKey[list[AppProcess]]()


# --- Postgres ------------------------------------------------------------------------


def _start_embedded_postgres(pgdata: Path) -> tuple[str, Callable[[], None]]:
    """pgserver's bundled Postgres, listening on TCP so the URL has the contract's
    `postgresql://user:pass@host:port/db` form (pgserver.get_server() only offers a unix socket)."""
    import pgserver  # only needed when TEST_DATABASE_URL is not set

    pgserver.initdb(
        ["--auth=trust", "--auth-local=trust", "--encoding=utf8", "--no-instructions", "-U", "postgres"],
        pgdata=pgdata,
        timeout=120,
    )
    port = free_port()
    pgserver.pg_ctl(
        ["-w", "-t", "60", "-o", f"-h 127.0.0.1 -p {port} -k ''", "-l", str(pgdata.parent / "postgres.log"), "start"],
        pgdata=pgdata,
        timeout=90,
    )

    def stop() -> None:
        pgserver.pg_ctl(["-w", "-m", "fast", "stop"], pgdata=pgdata, timeout=60)

    # trust auth: the password is accepted and ignored
    return f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres", stop


@pytest.fixture(scope="session")
def postgres_admin_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A server where tests may create databases: TEST_DATABASE_URL, or an embedded Postgres for the session."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    admin_url, stop = _start_embedded_postgres(tmp_path_factory.mktemp("postgres") / "pgdata")
    try:
        yield admin_url
    finally:
        stop()


@pytest.fixture
def database_url(postgres_admin_url: str) -> Iterator[str]:
    """A fresh, empty database for this test only."""
    name = f"todobot_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_admin_url, autocommit=True, connect_timeout=10) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield with_database(postgres_admin_url, name)
    finally:
        with psycopg.connect(postgres_admin_url, autocommit=True, connect_timeout=10) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def closed_port_database_url() -> str:
    """A DATABASE_URL whose server does not answer: nothing listens on that port."""
    return f"postgresql://postgres:postgres@127.0.0.1:{free_port()}/todobot"


# --- Fake Telegram Bot API -----------------------------------------------------------


@pytest.fixture
def fake_api() -> Iterator[FakeBotApi]:
    api = FakeBotApi().start()
    try:
        yield api
    finally:
        api.stop()


# --- The app -------------------------------------------------------------------------


@pytest.fixture
def launch_app(request: pytest.FixtureRequest, fake_api: FakeBotApi) -> Iterator[Callable[..., AppProcess]]:
    """Start `serve` (after `migrate` unless migrate=False) against a database URL; stopped after the test."""
    apps: list[AppProcess] = []
    request.node.stash[_APPS] = apps

    def launch(database_url: str, *, migrate: bool = True) -> AppProcess:
        port = free_port()
        env = app_env(database_url=database_url, port=port, api_base=fake_api.base_url)
        if migrate:
            run_migrate(env)
        app = AppProcess(env, port)
        apps.append(app)
        return app.start()

    try:
        yield launch
    finally:
        for app in apps:
            app.stop()


@pytest.fixture
def migrate(fake_api: FakeBotApi) -> Callable[[str], None]:
    """Run `uv run python -m todobot migrate` against a database URL, with the contract's environment."""

    def run(database_url: str) -> None:
        run_migrate(app_env(database_url=database_url, port=free_port(), api_base=fake_api.base_url))

    return run

# The app is launched inside each test body (not in a fixture), so an app that cannot start
# reports the test as FAILED with the app's output, rather than as a fixture ERROR.


# --- Reporting -----------------------------------------------------------------------


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    report = yield
    if report.when == "call" and report.failed:
        message = str(report.longrepr)
        for number, app in enumerate(item.stash.get(_APPS, []), 1):
            output = app.output()
            if output not in message:  # start failures and reply timeouts already carry it
                report.sections.append((f"todobot serve #{number} (port {app.port}) output", output))
    return report
