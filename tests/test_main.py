"""Tests for the GitHub webhook route's HTTP status mapping.

The load-bearing regression fix lives here: a handler failure must
answer a NON-2xx so GitHub's built-in delivery retry re-runs the
event. Before the fix the route caught every exception and still
returned 200, so a transient Plane-API blip during a batch-merge
silently dropped the ticket transition and was never retried.

The route is exercised without the app lifespan (we set
``app.state`` by hand and use ``TestClient`` outside its context
manager) so the test doesn't need real env vars or a config file.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.main as main_mod
from src.config import ClosedUnmergedRule, Config, ModulesConfig, StateMachine
from src.handler import PullRequestHandlerError
from src.main import app


def _config() -> Config:
    return Config(
        plane_base_url="https://plane.test",
        plane_workspace="ws",
        state_machine=StateMachine(
            opened_draft="In Progress",
            opened_ready="In Review",
            ready_for_review="In Review",
            converted_to_draft="In Progress",
            merged="Testing",
            closed_unmerged=ClosedUnmergedRule(if_state="In Review", set_to="In Progress"),
        ),
        modules=ModulesConfig(enabled=False),
    )


@pytest.fixture
def configured_app(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Populate the state the lifespan would normally set, then bypass
    # signature verification (the bytes aren't what's under test here).
    app.state.cfg = _config()
    app.state.plane = object()
    app.state.resolver = object()
    app.state.webhook_secret = "secret"
    app.state.last_webhook = {"at": None, "event": None, "status": None}
    app.state.webhook_count = 0
    monkeypatch.setattr(main_mod, "verify", lambda *a, **k: True)
    # No `with` → lifespan does not run, so our hand-set state stands.
    return TestClient(app)


def _post(client: TestClient, payload: dict) -> object:
    return client.post(
        "/webhook/github",
        json=payload,
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d1"},
    )


async def _ok_handler(*_a: object, **_k: object) -> None:
    return None


async def _failing_handler(*_a: object, **_k: object) -> None:
    raise PullRequestHandlerError("1/1 ref(s) failed for PR #42")


def test_handler_success_returns_200(
    configured_app: TestClient, monkeypatch: pytest.MonkeyPatch, pr_merged: dict
) -> None:
    monkeypatch.setattr(main_mod, "handle_pull_request", _ok_handler)
    resp = _post(configured_app, pr_merged)
    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"


def test_handler_failure_returns_503_for_github_retry(
    configured_app: TestClient, monkeypatch: pytest.MonkeyPatch, pr_merged: dict
) -> None:
    """The fix: a handler exception → 503, not 200. GitHub retries on
    non-2xx, which is what recovers the transient Plane-API failure."""
    monkeypatch.setattr(main_mod, "handle_pull_request", _failing_handler)
    resp = _post(configured_app, pr_merged)
    assert resp.status_code == 503
    # The failure detail still carries the PR context for diagnosis.
    assert resp.json()["detail"]["status"] == "handler_error"
    assert resp.json()["detail"]["pr"] == 42


def test_ping_event_acks_200(configured_app: TestClient) -> None:
    resp = configured_app.post(
        "/webhook/github",
        content=b"{}",
        headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "d0"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pong"
