"""SK-4 GET /healthz: 200 with the version when the database answers, 503 when it does not."""

from __future__ import annotations

import pytest

from harness import APP_VERSION

pytestmark = pytest.mark.acceptance


def test_sk4_healthz_returns_ok_and_version_when_database_answers(launch_app, database_url):
    app = launch_app(database_url)
    response = app.get("/healthz")

    assert response.status_code == 200, f"body: {response.text!r}"
    assert response.json() == {"status": "ok", "version": APP_VERSION}


def test_sk4_server_starts_and_healthz_returns_503_when_database_does_not_answer(
    launch_app, closed_port_database_url
):
    # launch_app fails the test unless `serve` starts and accepts connections.
    app = launch_app(closed_port_database_url, migrate=False)

    response = app.get("/healthz")

    assert response.status_code == 503, f"body: {response.text!r}"
    body = response.json()
    assert isinstance(body, dict) and body.get("status") == "error", f"body: {body!r}"
    assert app.is_running()
