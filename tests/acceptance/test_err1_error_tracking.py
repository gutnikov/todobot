"""ERR#1 Every ERROR line goes to PostHog's error tracking as one `$exception`, without what users write and with no
user id; nothing else a user does is sent to PostHog. Examples of slices S02 and A01 in agentify/specs/model.

State runningBotWithErrorTracking: `serve` runs with POSTHOG_PROJECT_TOKEN and POSTHOG_HOST pointing at a fake
PostHog that takes everything (err_harness.FakePostHog). The fake is read after the app has stopped, since the
process sends what it has queued before it exits.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from dup_harness import ANNA
from err_harness import (
    POSTHOG_WAIT,
    FakePostHog,
    carries,
    describe_failure,
    error_lines,
    serve_with_posthog,
)
from harness import APP_VERSION, ERROR_REPLY, SAVED_REPLY, deliver, find_records, text_update

pytestmark = pytest.mark.acceptance

TEXT = "Купить молоко и хлеб"
# What Анна wrote, whole and word by word, and her Telegram id: none of it may reach PostHog.
PRIVATE = [TEXT, "Купить", "молоко", "хлеб", str(ANNA.id)]


@pytest.fixture
def fake_posthog() -> Iterator[FakePostHog]:
    with FakePostHog() as posthog:
        yield posthog


def test_err1_saved_message_sends_posthog_nothing(fake_api, database_url, fake_posthog):
    """S02 «saved message sends PostHog nothing», state runningBotWithErrorTracking, PostHog takes (ERR#1)."""
    with serve_with_posthog(database_url, fake_api, fake_posthog) as app:
        delivery = deliver(app, fake_api, text_update(ANNA, TEXT))
        assert delivery.reply_text == SAVED_REPLY
        fake_posthog.wait_for_event("server_started", within=POSTHOG_WAIT)
        app.stop()  # the process sends what it has queued before it exits

        names = [event.get("event") for event in fake_posthog.events]
        assert names == ["server_started"], (
            f"PostHog got the events {names}; a saved message must send it nothing, so only the start's "
            f"server_started may be there\n{describe_failure(app, fake_posthog)}"
        )
        leaks = fake_posthog.received_any_of(PRIVATE)
        assert not leaks, (
            "the saved message reached PostHog: " + "; ".join(leaks) + f"\n{describe_failure(app, fake_posthog)}"
        )


def test_err1_handler_error_reaches_posthog_without_what_the_user_wrote(fake_api, database_url, fake_posthog):
    """A01 «handler error reaches PostHog without what the user wrote», state annaHitAHandlerError: Database
    storingFails, PostHog takes (ERR#1)."""
    with serve_with_posthog(database_url, fake_api, fake_posthog, migrate=False) as app:  # storing fails
        # The state annaHitAHandlerError: Анна sent «Купить молоко и хлеб», storing it failed, she got the apology.
        delivery = deliver(app, fake_api, text_update(ANNA, TEXT))
        assert delivery.reply_text == ERROR_REPLY
        fake_posthog.wait_for_event("$exception", within=POSTHOG_WAIT)
        lines = app.stop()  # the process sends what it has queued before it exits

        handler_errors = find_records(lines, level="ERROR", event="handler_error")
        assert len(handler_errors) == 1, (
            f"the state annaHitAHandlerError has one stdout line level=ERROR event=handler_error, there are "
            f"{len(handler_errors)}\n{app.output()}"
        )
        failure = describe_failure(app, fake_posthog)

        # $exception, once for the handler_error line (and one per ERROR line: ERR#1's "every")
        exceptions = fake_posthog.events_named("$exception")
        logged_errors = error_lines(lines)
        assert len(exceptions) == len(logged_errors), (
            f"PostHog got {len(exceptions)} $exception event(s) for the {len(logged_errors)} stdout line(s) with "
            f"level ERROR or CRITICAL; each such line goes to PostHog as one $exception\n{failure}"
        )
        # its properties carry the log event handler_error
        for_handler_error = [event for event in exceptions if carries(event.get("properties"), "handler_error")]
        assert len(for_handler_error) == 1, (
            f"{len(for_handler_error)} $exception event(s) carry the log event 'handler_error' among their "
            f"properties, expected exactly one\n{failure}"
        )
        exception = for_handler_error[0]
        properties = exception.get("properties") or {}

        # the error type, as the handler_error line names it
        error_class = handler_errors[0].get("error")
        assert isinstance(error_class, str), (
            f"the handler_error line names no error type in its `error` field: {handler_errors[0]!r}"
        )
        assert carries(properties, error_class), (
            f"the $exception's properties do not carry the error type {error_class!r} of the handler_error line"
            f"\n{failure}"
        )
        # no user id, no person profile, the release's version
        assert exception.get("distinct_id") == "todobot", (
            f"the $exception's distinct_id is {exception.get('distinct_id')!r}, expected 'todobot'\n{failure}"
        )
        assert properties.get("$process_person_profile") is False, (
            f"the $exception's properties have $process_person_profile="
            f"{properties.get('$process_person_profile', '(missing)')!r}, expected false\n{failure}"
        )
        assert carries(properties, APP_VERSION), (
            f"the $exception's properties do not carry the version {APP_VERSION!r}\n{failure}"
        )
        # nothing of what Анна wrote, and not her id, anywhere in what PostHog got
        leaks = fake_posthog.received_any_of(PRIVATE)
        assert not leaks, "PostHog got what the user wrote or her id: " + "; ".join(leaks) + f"\n{failure}"
