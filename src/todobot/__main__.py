"""`python -m todobot migrate|serve`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Callable, Sequence

from todobot.config import ConfigError, PostHogSettings, Settings, app_version, require
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


def _serve(on_started: Callable[[], None] | None = None) -> None:
    from todobot import web

    asyncio.run(web.serve(Settings.from_env(), on_started=on_started))


def _run(command: str, run: Callable[[], None]) -> int:
    """Run a command; its exit code. What escapes it is logged here, as the last resort."""
    try:
        run()
    except ConfigError as exc:
        log_event(logger, "config_error", level=logging.ERROR, error=str(exc))
        return 2
    except Exception as exc:
        log_event(logger, f"{command}_failed", level=logging.ERROR, exc_info=exc)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    version = app_version()
    setup_logging(version)
    if args.command == "migrate":
        return _run(args.command, _migrate)

    posthog_settings = PostHogSettings.from_env()
    if posthog_settings is None:  # without POSTHOG_PROJECT_TOKEN, serve sends PostHog nothing
        return _run(args.command, _serve)

    from todobot.error_tracking import errors_to_posthog

    # ERR: until serve exits, its config_error or serve_failed included, every ERROR or CRITICAL line also goes to
    # PostHog; what is queued is sent on the way out.
    with errors_to_posthog(posthog_settings, version) as tracking:
        return _run(args.command, lambda: _serve(on_started=tracking.server_started))


if __name__ == "__main__":
    sys.exit(main())
