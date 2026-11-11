"""ACC#2 `serve` says at start which way it runs: with the allow list set, a log line with event=access_restricted and
the number of users it names; without it, the bot serves every user and writes a line with level WARNING and
event=access_open. Slice S04's examples in agentify/specs/model.

States: closedBot (`serve` with TELEGRAM_ALLOWED_USER_IDS naming Anna, 5812345678, only) and runningBot (`serve`
without the variable).
"""

from __future__ import annotations

import pytest
from acc_harness import GIVE_UP_AFTER, OWNER_ONLY, serve_app, wait_for_records
from harness import find_records

pytestmark = pytest.mark.acceptance


def test_acc2_server_with_an_allow_list_says_it_is_restricted(fake_api, database_url):
    """S04 «server with an allow list says it is restricted», state closedBot, Database answers (ACC#2)."""
    with serve_app(database_url, fake_api, allowed_user_ids=OWNER_ONLY) as app:
        restricted = wait_for_records(app, event="access_restricted")

        assert restricted, (
            f"no stdout line with event=access_restricted within {GIVE_UP_AFTER:g} s of the start, with "
            f"TELEGRAM_ALLOWED_USER_IDS={OWNER_ONLY}\n{app.output()}"
        )
        assert all(record.get("allowed") == 1 for record in restricted), (
            f"the access_restricted line(s) say allowed={[record.get('allowed') for record in restricted]!r}; the "
            f"allow list names one user, expected allowed=1: {restricted!r}"
        )
        opened = find_records(app.stdout_lines, event="access_open")
        assert not opened, f"serve runs restricted but also wrote event=access_open: {opened!r}\n{app.output()}"


def test_acc2_server_without_an_allow_list_warns_that_it_is_open(fake_api, database_url):
    """S04 «server without an allow list warns that it is open», state runningBot, Database answers (ACC#2)."""
    with serve_app(database_url, fake_api, allowed_user_ids=None) as app:
        opened = wait_for_records(app, event="access_open")

        assert opened, (
            f"no stdout line with event=access_open within {GIVE_UP_AFTER:g} s of the start, without "
            f"TELEGRAM_ALLOWED_USER_IDS\n{app.output()}"
        )
        levels = [record.get("level") for record in opened]
        assert all(level == "WARNING" for level in levels), (
            f"the access_open line(s) have level {levels!r}, expected WARNING: {opened!r}"
        )
        restricted = find_records(app.stdout_lines, event="access_restricted")
        assert not restricted, f"serve runs open but also wrote event=access_restricted: {restricted!r}\n{app.output()}"
