"""Tests for ``src.module_state.desired_module_status`` — pure logic.

The function decides whether and how a module's status should change
in response to its work items' state groups. The tests are
exhaustive over the transition matrix because the rules are small
and the consequences (incorrect Plane mutations) are visible to the
operator.
"""

from __future__ import annotations

import pytest

from src.module_state import (
    STATUS_BACKLOG,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_PAUSED,
    STATUS_PLANNED,
    desired_module_status,
)

# Fake state UUIDs and their groups — same shape Plane returns from
# /states/.
GROUP_MAP = {
    "u-todo": "unstarted",
    "u-progress": "started",
    "u-review": "started",
    "u-testing": "started",
    "u-done": "completed",
    "u-cancelled": "cancelled",
    "u-backlog": "backlog",
}


# ── Planned / backlog → in-progress ─────────────────────────────────


class TestPromoteToInProgress:
    def test_planned_with_started_item_becomes_in_progress(self) -> None:
        assert (
            desired_module_status(
                item_state_uuids=["u-todo", "u-progress"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_PLANNED,
            )
            == STATUS_IN_PROGRESS
        )

    def test_backlog_with_started_item_becomes_in_progress(self) -> None:
        assert (
            desired_module_status(
                item_state_uuids=["u-progress"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_BACKLOG,
            )
            == STATUS_IN_PROGRESS
        )

    def test_planned_with_only_unstarted_items_does_not_promote(self) -> None:
        assert (
            desired_module_status(
                item_state_uuids=["u-todo", "u-backlog"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_PLANNED,
            )
            is None
        )

    def test_review_and_testing_also_count_as_started(self) -> None:
        # Any state in the 'started' group should trigger the promotion.
        for in_progress_state in ("u-progress", "u-review", "u-testing"):
            result = desired_module_status(
                item_state_uuids=["u-todo", in_progress_state],
                state_to_group=GROUP_MAP,
                current_status=STATUS_PLANNED,
            )
            assert result == STATUS_IN_PROGRESS, in_progress_state

    def test_already_in_progress_does_not_re_promote(self) -> None:
        assert (
            desired_module_status(
                item_state_uuids=["u-progress"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_IN_PROGRESS,
            )
            is None
        )


# ── Completion ─────────────────────────────────────────────────────


class TestPromoteToCompleted:
    def test_all_completed_items_finishes_the_module(self) -> None:
        assert (
            desired_module_status(
                item_state_uuids=["u-done", "u-done", "u-done"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_IN_PROGRESS,
            )
            == STATUS_COMPLETED
        )

    def test_all_cancelled_items_also_completes_module(self) -> None:
        # Per the explicit rule "complete when all are cancelled or
        # completed", an all-cancelled module is "complete" — no more
        # work to do, regardless of why.
        assert (
            desired_module_status(
                item_state_uuids=["u-cancelled", "u-cancelled"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_IN_PROGRESS,
            )
            == STATUS_COMPLETED
        )

    def test_mixed_terminal_items_completes_module(self) -> None:
        assert (
            desired_module_status(
                item_state_uuids=["u-done", "u-cancelled", "u-done"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_PLANNED,
            )
            == STATUS_COMPLETED
        )

    def test_one_non_terminal_item_blocks_completion(self) -> None:
        assert (
            desired_module_status(
                item_state_uuids=["u-done", "u-done", "u-progress"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_IN_PROGRESS,
            )
            is None
        )

    def test_idempotent_when_already_completed(self) -> None:
        # If somebody calls in on a module that's already completed,
        # return None (no-op) rather than re-emitting "completed".
        assert (
            desired_module_status(
                item_state_uuids=["u-done"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_COMPLETED,
            )
            is None
        )


# ── Locked-status guards ───────────────────────────────────────────


class TestLockedStatuses:
    @pytest.mark.parametrize("locked", [STATUS_PAUSED, STATUS_CANCELLED])
    def test_locked_modules_are_never_changed(self, locked: str) -> None:
        # paused + cancelled are the only operator-managed terminals.
        # Completed is auto-reversible — see TestReopenFromCompleted.
        for items in (
            [],
            ["u-progress"],
            ["u-done", "u-done"],
            ["u-cancelled", "u-cancelled"],
            ["u-progress", "u-done"],
        ):
            result = desired_module_status(
                item_state_uuids=items,
                state_to_group=GROUP_MAP,
                current_status=locked,
            )
            assert result is None, (locked, items)


# ── Reopen / new-item-added from completed → in-progress ────────────


class TestReopenFromCompleted:
    """A completed module is auto-reversible: any non-terminal item
    in the module (reopened existing item OR newly added item) flips
    the module back to in-progress so dashboards stay accurate."""

    def test_reopened_item_started_flips_module_back(self) -> None:
        # One ticket moved from Done back into the started group
        # (typical reopen scenario).
        assert (
            desired_module_status(
                item_state_uuids=["u-done", "u-progress", "u-done"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_COMPLETED,
            )
            == STATUS_IN_PROGRESS
        )

    def test_new_backlog_item_added_flips_module_back(self) -> None:
        # A fresh ticket added to a previously-done module — Plane
        # default-states it to backlog, not started. Module should
        # still wake up.
        assert (
            desired_module_status(
                item_state_uuids=["u-done", "u-done", "u-backlog"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_COMPLETED,
            )
            == STATUS_IN_PROGRESS
        )

    def test_new_unstarted_item_added_flips_module_back(self) -> None:
        # Same idea for the Todo / unstarted group.
        assert (
            desired_module_status(
                item_state_uuids=["u-done", "u-todo"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_COMPLETED,
            )
            == STATUS_IN_PROGRESS
        )

    def test_item_with_no_state_still_flips_module_back(self) -> None:
        # Brand-new ticket with state=None should be treated as
        # non-terminal and wake the module up.
        assert (
            desired_module_status(
                item_state_uuids=["u-done", None],
                state_to_group=GROUP_MAP,
                current_status=STATUS_COMPLETED,
            )
            == STATUS_IN_PROGRESS
        )

    def test_all_terminal_keeps_completed_idempotent(self) -> None:
        # Completed module + every item still terminal → no-op (covered
        # in TestPromoteToCompleted but re-asserted here so the
        # reopen test class is self-contained).
        assert (
            desired_module_status(
                item_state_uuids=["u-done", "u-cancelled"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_COMPLETED,
            )
            is None
        )

    def test_paused_completed_module_stays_paused(self) -> None:
        # Sanity check: even if the module was once completed and is
        # now paused, paused wins (paused is locked).
        assert (
            desired_module_status(
                item_state_uuids=["u-done", "u-progress"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_PAUSED,
            )
            is None
        )

    def test_completed_module_with_only_unknown_uuids_still_flips(self) -> None:
        # Unknown UUIDs are treated as unstarted (non-terminal). A
        # completed module sees them as work-to-do and wakes up.
        assert (
            desired_module_status(
                item_state_uuids=["u-some-new-state"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_COMPLETED,
            )
            == STATUS_IN_PROGRESS
        )


# ── Edge cases ─────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_module_is_left_alone(self) -> None:
        # No items → no transition; can't distinguish "freshly created"
        # from "all items moved out". Operator manages.
        assert (
            desired_module_status(
                item_state_uuids=[],
                state_to_group=GROUP_MAP,
                current_status=STATUS_PLANNED,
            )
            is None
        )

    def test_none_current_status_defaults_to_planned(self) -> None:
        # A module with no `status` field set (older record) is
        # treated as `planned`, so the promote-to-in-progress rule
        # still fires.
        assert (
            desired_module_status(
                item_state_uuids=["u-progress"],
                state_to_group=GROUP_MAP,
                current_status=None,
            )
            == STATUS_IN_PROGRESS
        )

    def test_unknown_state_uuid_treated_as_unstarted(self) -> None:
        # A state UUID we've never seen (e.g. a brand-new state added
        # to the project but not yet refreshed in our cache) is
        # treated as non-terminal + non-started — same as backlog.
        assert (
            desired_module_status(
                item_state_uuids=["u-progress", "u-unknown-state"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_PLANNED,
            )
            == STATUS_IN_PROGRESS
        )

    def test_none_state_uuid_treated_as_unstarted(self) -> None:
        # Work items occasionally arrive with state=None (newly-
        # created records); skip them rather than crash.
        assert (
            desired_module_status(
                item_state_uuids=[None, "u-progress"],
                state_to_group=GROUP_MAP,
                current_status=STATUS_PLANNED,
            )
            == STATUS_IN_PROGRESS
        )

    def test_completed_status_input_is_case_insensitive(self) -> None:
        # Plane sometimes returns title-cased status strings; the
        # reconciler accepts both.
        assert (
            desired_module_status(
                item_state_uuids=["u-progress"],
                state_to_group=GROUP_MAP,
                current_status="Planned",
            )
            == STATUS_IN_PROGRESS
        )
