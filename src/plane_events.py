"""Parse Plane CE webhook payloads.

Plane fires webhooks for every domain object — issues, modules,
cycles, projects, comments, etc. — with the same envelope shape:

.. code-block:: json

    {
      "event": "issue",
      "action": "updated",
      "data": { ...the object that changed... },
      "workspace_id": "...",
      "project_id": "..."
    }

pylon only acts on **issue state changes** today: when an issue
``updated`` event comes in, we reconcile the modules that issue
belongs to (in case the change was a state transition that flipped
the module from ``planned`` → ``in-progress`` or completed it).

Other event types (module updates, cycle changes, comments, etc.)
are recognised but mapped to :data:`Action.IGNORED` so the webhook
endpoint can ack 200 and pylon logs without acting.

The parser is intentionally permissive about payload shape — Plane
CE's webhook payload has evolved across versions, and being strict
would silently break on upgrade. Missing fields produce a
``ParsedPlaneEvent`` with ``action="ignored"`` rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# Actions we care about at the pylon layer. ``issue_updated`` covers
# any change to a work item (state, title, labels, assignees, …);
# the reconciler is idempotent so we trigger on the union.
Action = Literal["issue_updated", "ignored"]


@dataclass(frozen=True)
class ParsedPlaneEvent:
    """Distilled event pylon cares about."""

    action: Action
    work_item_id: str | None
    project_id: str | None
    module_ids: tuple[str, ...]
    # The raw event/action strings for logging — useful when we
    # ignore an event so the operator can see what came in.
    raw_event: str
    raw_action: str


def parse_plane_event(payload: dict) -> ParsedPlaneEvent:
    """Decode a Plane webhook payload into a typed event.

    Returns a record with ``action="ignored"`` for anything that
    isn't an actionable issue-update. The endpoint logs+200's
    ignored events without invoking the handler.
    """
    raw_event = _coerce_str(payload.get("event") or payload.get("type") or "")
    raw_action = _coerce_str(payload.get("action") or "")

    if raw_event != "issue":
        return _ignored(raw_event, raw_action)

    # Treat both created and updated as triggers — a newly-created
    # ticket dropped into a module should also re-check the module
    # status. Plane's "updated" covers retitle / restate / move-to-
    # module; "created" covers the initial drop.
    if raw_action not in ("created", "updated"):
        return _ignored(raw_event, raw_action)

    data = payload.get("data")
    if not isinstance(data, dict):
        return _ignored(raw_event, raw_action)

    work_item_id = _first_str(data.get("id"), data.get("issue_id"))
    project_id = _first_str(
        data.get("project_id"),
        data.get("project"),
        payload.get("project_id"),
        payload.get("project"),
    )
    raw_modules = data.get("module_ids") or data.get("modules") or []
    module_ids = tuple(
        m for m in raw_modules if isinstance(m, str) and m
    )

    return ParsedPlaneEvent(
        action="issue_updated",
        work_item_id=work_item_id,
        project_id=project_id,
        module_ids=module_ids,
        raw_event=raw_event,
        raw_action=raw_action,
    )


# ── Helpers ────────────────────────────────────────────────────────


def _ignored(event: str, action: str) -> ParsedPlaneEvent:
    return ParsedPlaneEvent(
        action="ignored",
        work_item_id=None,
        project_id=None,
        module_ids=(),
        raw_event=event,
        raw_action=action,
    )


def _coerce_str(v: object) -> str:
    return v.strip() if isinstance(v, str) else ""


def _first_str(*candidates: object) -> str | None:
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return None
