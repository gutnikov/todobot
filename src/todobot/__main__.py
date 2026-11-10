"""`python -m todobot migrate|serve`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence

from todobot.config import ConfigError, Settings, app_version, require
from todobot.logs import log_event, setup_logging

logger = logging.getLogger("todobot")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m todobot", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("migrate", help="apply the database migrations to DATABASE_URL and exit")
    commands.add_parser("serve", help="serve the webhook and /healthz on 0.0.0.0:PORT")
    return parser


def _migrate() -> None:
    from todobot import migrate

    database_url = require(os.environ, "DATABASE_URL")
    log_event(logger, "migrate_started")
    asyncio.run(migrate.upgrade(database_url))
    log_event(logger, "migrate_finished")


def _serve() -> None:
    from todobot import web

    asyncio.run(web.serve(Settings.from_env()))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging(app_version())
    try:
        if args.command == "migrate":
            _migrate()
        else:
            _serve()
    except ConfigError as exc:
        log_event(logger, "config_error", level=logging.ERROR, error=str(exc))
        return 2
    except Exception as exc:
        log_event(logger, f"{args.command}_failed", level=logging.ERROR, exc_info=exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
