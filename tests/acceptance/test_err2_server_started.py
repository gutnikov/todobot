"""ERR#2 When `serve` starts, it sends PostHog one event `server_started` with the release's version. Slice S04's
example in agentify/specs/model.

State runningBotWithErrorTracking: `serve` runs with POSTHOG_PROJECT_TOKEN and POSTHOG_HOST pointing at a fake
PostHog that takes everything (err_harness.FakePostHog).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from err_harness import POSTHOG_WAIT, FakePostHog, carries, describe_failure, serve_with_posthog
from harness import APP_VERSION

pytestmark = pytest.mark.acceptance


@pytest.fixture
def fake_posthog() -> Iterator[FakePostHog]:
    with FakePostHog() as posthog:
        yield posthog


def test_err2_server_start_is_announced_to_posthog_with_the_version(fake_api, database_url, fake_posthog):
    """S04 «server start is announced to PostHog with the version», state runningBotWithErrorTracking, Database
    answers, PostHog takes (ERR#2)."""
    with serve_with_posthog(database_url, fake_api, fake_posthog) as app:
        fake_posthog.wait_for_event("server_started", within=POSTHOG_WAIT)
        app.stop()  # the process sends what it has queued before it exits
    failure = describe_failure(app, fake_posthog)

    started = fake_posthog.events_named("server_started")
    assert len(started) == 1, f"PostHog got {len(started)} server_started event(s), expected one\n{failure}"
    event = started[0]
    properties = event.get("properties") or {}
    assert carries(properties, APP_VERSION), (
        f"server_started's properties do not carry the version {APP_VERSION!r}\n{failure}"
    )
    assert event.get("distinct_id") == "todobot", (
        f"server_started's distinct_id is {event.get('distinct_id')!r}, expected 'todobot'\n{failure}"
    )
    assert properties.get("$process_person_profile") is False, (
        f"server_started's properties have $process_person_profile="
        f"{properties.get('$process_person_profile', '(missing)')!r}, expected false\n{failure}"
    )
