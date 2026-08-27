"""Tests for the Forgejo webhook route.

Forgejo fires GitHub-compatible pull_request payloads, so the route reuses
the GitHub parser and the shared PR->Plane handler. These tests cover the
Forgejo-specific surface: the X-Forgejo-Event header, the ping ack, the
ignored-event path, and the same success-200 / handler-failure-503 mapping
the GitHub route guarantees (so Forgejo's delivery retry recovers a
transient Plane-API blip).

Signature verification is exercised by passing an empty
``forgejo_webhook_secret`` (the documented "accept unsigned until wired up"
mode), so the route skips verify() and the payload bytes aren't under test.
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
def configured_app() -> TestClient:
    app.state.cfg = _config()
    app.state.plane = object()
    app.state.resolver = object()
    # Empty secret → route accepts unsigned deliveries (skips verify()).
    app.state.forgejo_webhook_secret = ""
    app.state.last_webhook = {"at": None, "event": None, "status": None}
    app.state.webhook_count = 0
    return TestClient(app)


def _post(client: TestClient, payload: dict) -> object:
    return client.post(
        "/webhook/forgejo",
        json=payload,
        headers={"X-Forgejo-Event": "pull_request"},
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
    assert resp.json()["pr"] == 42


def test_handler_failure_returns_503_for_forgejo_retry(
    configured_app: TestClient, monkeypatch: pytest.MonkeyPatch, pr_merged: dict
) -> None:
    monkeypatch.setattr(main_mod, "handle_pull_request", _failing_handler)
    resp = _post(configured_app, pr_merged)
    assert resp.status_code == 503
    assert resp.json()["detail"]["status"] == "handler_error"
    assert resp.json()["detail"]["pr"] == 42


def test_ping_event_acks_200(configured_app: TestClient) -> None:
    resp = configured_app.post(
        "/webhook/forgejo",
        content=b"{}",
        headers={"X-Forgejo-Event": "ping"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pong"


def test_gitea_event_header_fallback_ignored_event(configured_app: TestClient) -> None:
    # Forgejo also sends the Gitea-compatible header; a non-PR event acks 200.
    resp = configured_app.post(
        "/webhook/forgejo",
        content=b"{}",
        headers={"X-Gitea-Event": "push"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
