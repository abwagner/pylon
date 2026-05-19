"""Tests for ``src.plane_events.parse_plane_event``."""

import pytest

from src.plane_events import ParsedPlaneEvent, parse_plane_event


# ── Issue events ───────────────────────────────────────────────────


class TestIssueUpdate:
    def test_extracts_id_project_modules(self) -> None:
        event = parse_plane_event(
            {
                "event": "issue",
                "action": "updated",
                "data": {
                    "id": "wi-uuid-12",
                    "project_id": "prj-uuid",
                    "module_ids": ["mod-A", "mod-B"],
                },
            }
        )
        assert event.action == "issue_updated"
        assert event.work_item_id == "wi-uuid-12"
        assert event.project_id == "prj-uuid"
        assert event.module_ids == ("mod-A", "mod-B")

    def test_issue_created_also_triggers_reconcile(self) -> None:
        # A new ticket dropped straight into a module should re-check
        # the module's status — treat created the same as updated.
        event = parse_plane_event(
            {
                "event": "issue",
                "action": "created",
                "data": {
                    "id": "wi-new",
                    "project_id": "prj",
                    "module_ids": ["mod-A"],
                },
            }
        )
        assert event.action == "issue_updated"

    def test_falls_back_to_top_level_project_id(self) -> None:
        # Some Plane releases put project_id at the envelope level
        # rather than inside `data`.
        event = parse_plane_event(
            {
                "event": "issue",
                "action": "updated",
                "project_id": "prj-uuid",
                "data": {
                    "id": "wi-uuid",
                    "module_ids": [],
                },
            }
        )
        assert event.project_id == "prj-uuid"

    def test_empty_module_ids_handled(self) -> None:
        event = parse_plane_event(
            {
                "event": "issue",
                "action": "updated",
                "data": {"id": "wi-uuid", "project_id": "prj", "module_ids": []},
            }
        )
        assert event.action == "issue_updated"
        assert event.module_ids == ()

    def test_missing_module_ids_handled(self) -> None:
        event = parse_plane_event(
            {
                "event": "issue",
                "action": "updated",
                "data": {"id": "wi-uuid", "project_id": "prj"},
            }
        )
        assert event.module_ids == ()

    def test_non_string_module_ids_filtered(self) -> None:
        # Defensive: a malformed module_ids entry shouldn't crash.
        event = parse_plane_event(
            {
                "event": "issue",
                "action": "updated",
                "data": {
                    "id": "wi-uuid",
                    "project_id": "prj",
                    "module_ids": ["mod-A", None, 42, "", "mod-B"],
                },
            }
        )
        assert event.module_ids == ("mod-A", "mod-B")


# ── Ignored events ────────────────────────────────────────────────


class TestIgnored:
    @pytest.mark.parametrize("event_type", ["module", "cycle", "project", "comment"])
    def test_non_issue_events_are_ignored(self, event_type: str) -> None:
        event = parse_plane_event(
            {"event": event_type, "action": "updated", "data": {"id": "x"}}
        )
        assert event.action == "ignored"
        assert event.raw_event == event_type
        assert event.raw_action == "updated"

    @pytest.mark.parametrize("action", ["deleted", "archived", ""])
    def test_non_create_update_actions_are_ignored(self, action: str) -> None:
        event = parse_plane_event(
            {"event": "issue", "action": action, "data": {"id": "x"}}
        )
        assert event.action == "ignored"
        # raw fields are preserved for log diagnostics.
        assert event.raw_event == "issue"
        assert event.raw_action == action

    def test_payload_without_data_object_is_ignored(self) -> None:
        # `data` missing entirely.
        assert (
            parse_plane_event({"event": "issue", "action": "updated"}).action
            == "ignored"
        )
        # `data` not an object.
        assert (
            parse_plane_event(
                {"event": "issue", "action": "updated", "data": [1, 2, 3]}
            ).action
            == "ignored"
        )

    def test_empty_payload_is_ignored(self) -> None:
        assert parse_plane_event({}).action == "ignored"


class TestType:
    def test_return_type(self) -> None:
        out = parse_plane_event({"event": "issue", "action": "updated"})
        assert isinstance(out, ParsedPlaneEvent)
        assert isinstance(out.module_ids, tuple)
