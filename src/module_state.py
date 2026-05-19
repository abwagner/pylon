"""Module-state reconciliation logic — pure functions.

Plane's module records carry a ``status`` field that can take one of
the strings ``backlog``, ``planned``, ``in-progress``, ``paused``,
``completed``, ``cancelled``. Pylon drives three of those transitions
automatically based on the work items the module contains:

* **planned / backlog → in-progress** as soon as a single work item
  in the module enters the ``started`` state group (so "I started
  working on something" flips the module's status without the
  operator having to remember).
* **anything except paused / cancelled → completed** as soon as
  every work item in the module is in the ``completed`` or
  ``cancelled`` state group AND the module has at least one work
  item.
* **completed → in-progress** when at least one work item is no
  longer terminal — either an existing item was reopened (state
  moved back out of completed/cancelled) or a new item was added
  to a module that had been all-terminal.

``paused`` and ``cancelled`` are the only operator-managed terminal
states; the reconciler never touches a module already in one of
those. Completed is auto-reversible because "no more work to do"
flips back to "more work to do" whenever a non-terminal item
appears.

The logic is split out as a pure function so the handler can call
it after each ticket-state transition and the unit tests can drive
it through every transition matrix without touching the API.
"""

from __future__ import annotations

# Module-status strings Plane accepts (and the ones this reconciler
# emits). Kept as constants so the call sites read as English.
STATUS_BACKLOG = "backlog"
STATUS_PLANNED = "planned"
STATUS_IN_PROGRESS = "in-progress"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

# Module statuses the reconciler will not touch — operator-managed
# terminal / hold states. ``completed`` is NOT locked: a completed
# module that picks up a non-terminal item flips back to
# ``in-progress``.
LOCKED_STATUSES: frozenset[str] = frozenset(
    {STATUS_PAUSED, STATUS_CANCELLED},
)

# State groups Plane CE always exposes.
GROUP_BACKLOG = "backlog"
GROUP_UNSTARTED = "unstarted"
GROUP_STARTED = "started"
GROUP_COMPLETED = "completed"
GROUP_CANCELLED = "cancelled"

# Terminal item groups — counted toward "module complete?"
TERMINAL_ITEM_GROUPS: frozenset[str] = frozenset(
    {GROUP_COMPLETED, GROUP_CANCELLED},
)


def desired_module_status(
    item_state_uuids: list[str | None],
    state_to_group: dict[str, str],
    current_status: str | None,
) -> str | None:
    """Compute the module status this work-item list implies.

    Parameters
    ----------
    item_state_uuids:
        State UUID for each work item in the module. ``None`` entries
        are tolerated (a brand-new item with no state assigned yet)
        and treated as "non-terminal, non-started" — same as a backlog
        item.
    state_to_group:
        Map from state UUID → group string (``backlog`` / ``unstarted``
        / ``started`` / ``completed`` / ``cancelled``). UUIDs not in
        the map are treated as ``unstarted``.
    current_status:
        The module's current ``status`` field. ``None`` is treated as
        ``"planned"`` (the most permissive default).

    Returns
    -------
    str | None
        The new status string to PATCH onto the module, or ``None``
        if no change is warranted (the module is empty, already in a
        locked state, or no transition rule fires).
    """
    current = (current_status or STATUS_PLANNED).strip().lower()
    if current in LOCKED_STATUSES:
        return None
    if not item_state_uuids:
        # An empty module never transitions; we can't know whether
        # zero items means "module just created" or "all items moved
        # away". Operator manages.
        return None

    groups = [state_to_group.get(u or "", GROUP_UNSTARTED) for u in item_state_uuids]
    all_terminal = all(g in TERMINAL_ITEM_GROUPS for g in groups)

    if all_terminal:
        if current == STATUS_COMPLETED:
            return None  # idempotent — caller would no-op anyway
        return STATUS_COMPLETED

    # At least one item is in backlog/unstarted/started. A previously
    # completed module flips back to in-progress on any such item —
    # covers both reopens (started item) and new tickets added to a
    # done module (backlog/unstarted item).
    if current == STATUS_COMPLETED:
        return STATUS_IN_PROGRESS

    if current in (STATUS_PLANNED, STATUS_BACKLOG) and any(
        g == GROUP_STARTED for g in groups
    ):
        return STATUS_IN_PROGRESS
    return None
