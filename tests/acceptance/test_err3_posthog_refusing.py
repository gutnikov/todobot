"""ERR#3 When PostHog does not take what the bot sends it, the bot goes on as before: a WARNING line `posthog_failed`,
no line of level ERROR for it, and replies and stores as usual. Slice S04's example in agentify/specs/model.

State runningBotWithErrorTracking with PostHog refusing: `serve` runs with POSTHOG_PROJECT_TOKEN and POSTHOG_HOST
pointing at a fake PostHog that answers every request with 500 (err_harness.FakePostHog(refuse=True)).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from dup_harness import ANNA
from err_harness import POSTHOG_WAIT, FakePostHog, describe_failure, error_lines, serve_with_posthog, wait_for_log_line
from harness import SAVED_REPLY, deliver, find_records, text_update

pytestmark = pytest.mark.acceptance


@pytest.fixture
def refusing_posthog() -> Iterator[FakePostHog]:
    with FakePostHog(refuse=True) as posthog:
        yield posthog


def _is_json_object(line: str) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


def test_err3_posthog_refusing_leaves_the_bot_running(fake_api, database_url, refusing_posthog):
    """S04 «PostHog refusing leaves the bot running», state runningBotWithErrorTracking, Database answers, PostHog
    refuses (ERR#3)."""
    with serve_with_posthog(database_url, fake_api, refusing_posthog) as app:
        # Let PostHog refuse what the start sends; the process may also report it only when it exits (below).
        wait_for_log_line(app, within=POSTHOG_WAIT, event="posthog_failed")

        # The bot goes on as before: a message Анна sends after the start is saved and acknowledged, /healthz answers.
        delivery = deliver(app, fake_api, text_update(ANNA, "Купить молоко и хлеб"))
        assert delivery.reply_text == SAVED_REPLY
        health = app.get("/healthz")
        assert health.status_code == 200, f"/healthz answered {health.status_code}: {health.text!r}\n{app.output()}"
        assert app.is_running(), f"serve exited with code {app.returncode} while PostHog refused\n{app.output()}"

        lines = app.stop()  # the process sends what it has queued before it exits
    failure = describe_failure(app, refusing_posthog)

    failed = find_records(lines, event="posthog_failed")
    assert failed, f"no stdout line with event=posthog_failed while PostHog answered 500\n{failure}"
    levels = [record.get("level") for record in failed]
    assert all(level == "WARNING" for level in levels), (
        f"the posthog_failed line(s) have level {levels}, expected WARNING\n{failure}"
    )
    unreadable = [line for line in lines if not _is_json_object(line)]
    assert not unreadable, f"stdout has lines whose level cannot be told (not JSON objects): {unreadable!r}\n{failure}"
    errors = error_lines(lines)
    assert not errors, f"stdout has line(s) with level ERROR or CRITICAL while PostHog refused: {errors!r}\n{failure}"
