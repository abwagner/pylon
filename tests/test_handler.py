from typing import Any

import pytest

from src.config import ClosedUnmergedRule, Config, ModulesConfig, StateMachine
from src.github_events import parse_pull_request
from src.handler import (
    PullRequestHandlerError,
    handle_plane_event,
    handle_pull_request,
)
from src.plane_events import parse_plane_event
from src.resolver import Resolver


PROJECT_UUID = "uuid-qf"
PT_UUID = "uuid-pt"

QF_PROJECTS = [
    {"id": PROJECT_UUID, "identifier": "QF"},
    {"id": PT_UUID, "identifier": "PT"},
]
QF_STATES = [
    {"id": "qf-todo", "name": "Todo", "group": "unstarted"},
    {"id": "qf-progress", "name": "In Progress", "group": "started"},
    {"id": "qf-review", "name": "In Review", "group": "started"},
    {"id": "qf-testing", "name": "Testing", "group": "started"},
    {"id": "qf-done", "name": "Done", "group": "completed"},
    {"id": "qf-cancelled", "name": "Cancelled", "group": "cancelled"},
]


def _config(*, modules_enabled: bool = False) -> Config:
    return Config(
        plane_base_url="https://plane.test",
        plane_workspace="example-workspace",
        state_machine=StateMachine(
            opened_draft="In Progress",
            opened_ready="In Review",
            ready_for_review="In Review",
            converted_to_draft="In Progress",
            merged="Testing",
            closed_unmerged=ClosedUnmergedRule(if_state="In Review", set_to="In Progress"),
        ),
        # Existing tests pre-date module reconciliation, so default
        # to off here. The dedicated module tests below opt back in.
        modules=ModulesConfig(enabled=modules_enabled),
    )


class FakePlane:
    """Implements both PlaneAPI (for handler) and the resolver's API surface."""

    def __init__(
        self,
        work_items: dict[tuple[str, int], dict],
        projects: list[dict] = QF_PROJECTS,
        states_by_project: dict[str, list[dict]] | None = None,
        modules: dict[str, dict] | None = None,
        module_items: dict[str, list[dict]] | None = None,
    ) -> None:
        self.work_items = work_items
        self.projects = projects
        self.states_by_project = states_by_project or {PROJECT_UUID: QF_STATES, PT_UUID: QF_STATES}
        self.calls: list[tuple[Any, ...]] = []
        self.links: dict[str, list[str]] = {}
        # Module store — keyed by module UUID.
        self.modules: dict[str, dict] = modules or {}
        self.module_items: dict[str, list[dict]] = module_items or {}

    async def list_projects(self) -> list[dict]:
        return self.projects

    async def list_states(self, project_id: str) -> list[dict]:
        return self.states_by_project.get(project_id, [])

    async def get_work_item_by_sequence(self, project_id: str, sequence: int) -> dict | None:
        return self.work_items.get((project_id, sequence))

    async def list_modules(self, project_id: str) -> list[dict]:
        self.calls.append(("list_modules", project_id))
        return list(self.modules.values())

    async def update_state(self, project_id: str, work_item_id: str, state_uuid: str) -> None:
        self.calls.append(("update_state", work_item_id, state_uuid))
        # Reflect the new state in the module's items list so subsequent
        # reconcile reads see it — same as Plane would after a PATCH.
        for items in self.module_items.values():
            for it in items:
                if it.get("id") == work_item_id:
                    it["state"] = state_uuid

    async def add_link(
        self, project_id: str, work_item_id: str, link_url: str, title: str = ""
    ) -> bool:
        existing = self.links.setdefault(work_item_id, [])
        if link_url in existing:
            self.calls.append(("add_link_dup", work_item_id, link_url))
            return False
        existing.append(link_url)
        self.calls.append(("add_link", work_item_id, link_url))
        return True

    async def add_comment(self, project_id: str, work_item_id: str, comment_html: str) -> None:
        self.calls.append(("add_comment", work_item_id, comment_html))

    async def get_module(self, project_id: str, module_id: str) -> dict | None:
        self.calls.append(("get_module", module_id))
        return self.modules.get(module_id)

    async def list_module_work_items(
        self, project_id: str, module_id: str
    ) -> list[dict]:
        self.calls.append(("list_module_work_items", module_id))
        return list(self.module_items.get(module_id, []))

    async def update_module_status(
        self, project_id: str, module_id: str, status: str
    ) -> None:
        self.calls.append(("update_module_status", module_id, status))
        if module_id in self.modules:
            self.modules[module_id]["status"] = status


@pytest.fixture
def cfg() -> Config:
    return _config()


def _wi(uuid: str, seq: int, state_uuid: str) -> dict[str, Any]:
    return {"id": uuid, "sequence_id": seq, "state": state_uuid}


async def test_pr_opened_ready_transitions_to_in_review_and_attaches_link(
    pr_opened: dict, cfg: Config
) -> None:
    pr_opened["pull_request"]["body"] = "[QF-12]"
    plane = FakePlane({(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-todo")})
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert ("update_state", "wi-12", "qf-review") in plane.calls
    assert ("add_link", "wi-12", "https://github.com/octocat/hello-world/pull/42") in plane.calls


async def test_pr_opened_draft_transitions_to_in_progress(
    pr_opened: dict, cfg: Config
) -> None:
    pr_opened["pull_request"]["draft"] = True
    pr_opened["pull_request"]["body"] = "[QF-12]"
    plane = FakePlane({(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-todo")})
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert ("update_state", "wi-12", "qf-progress") in plane.calls
    assert ("add_link", "wi-12", "https://github.com/octocat/hello-world/pull/42") in plane.calls


async def test_ready_for_review_transitions_to_in_review(
    pr_opened: dict, cfg: Config
) -> None:
    pr_opened["action"] = "ready_for_review"
    pr_opened["pull_request"]["draft"] = False
    pr_opened["pull_request"]["body"] = "[QF-12]"
    plane = FakePlane({(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-progress")})
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert ("update_state", "wi-12", "qf-review") in plane.calls
    assert ("add_link", "wi-12", "https://github.com/octocat/hello-world/pull/42") in plane.calls


async def test_converted_to_draft_transitions_to_in_progress_no_link(
    pr_opened: dict, cfg: Config
) -> None:
    pr_opened["action"] = "converted_to_draft"
    pr_opened["pull_request"]["draft"] = True
    pr_opened["pull_request"]["body"] = "[QF-12]"
    plane = FakePlane({(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-review")})
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert ("update_state", "wi-12", "qf-progress") in plane.calls
    assert all(c[0] != "add_link" for c in plane.calls)


async def test_pr_merged_transitions_to_testing(pr_merged: dict, cfg: Config) -> None:
    plane = FakePlane({(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-review")})
    event = parse_pull_request(pr_merged)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert plane.calls == [("update_state", "wi-12", "qf-testing")]


async def test_closed_unmerged_from_in_review_reverts_to_in_progress_and_comments(
    pr_closed_unmerged: dict, cfg: Config
) -> None:
    plane = FakePlane({(PROJECT_UUID, 13): _wi("wi-13", 13, "qf-review")})
    event = parse_pull_request(pr_closed_unmerged)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert ("update_state", "wi-13", "qf-progress") in plane.calls
    assert any(c[0] == "add_comment" for c in plane.calls)


async def test_closed_unmerged_from_other_state_no_state_change_but_comment(
    pr_closed_unmerged: dict, cfg: Config
) -> None:
    plane = FakePlane({(PROJECT_UUID, 13): _wi("wi-13", 13, "qf-progress")})
    event = parse_pull_request(pr_closed_unmerged)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert all(c[0] != "update_state" for c in plane.calls)
    assert any(c[0] == "add_comment" for c in plane.calls)


async def test_unknown_project_logs_and_skips(pr_merged: dict, cfg: Config) -> None:
    pr_merged["pull_request"]["body"] = "[WEB-1]"
    plane = FakePlane({})
    event = parse_pull_request(pr_merged)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert plane.calls == []


async def test_unknown_ticket_in_known_project_skips(pr_merged: dict, cfg: Config) -> None:
    pr_merged["pull_request"]["body"] = "[QF-99999]"
    plane = FakePlane({})
    event = parse_pull_request(pr_merged)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert plane.calls == []


async def test_missing_state_in_project_logs_and_skips(pr_merged: dict, cfg: Config) -> None:
    """If a Plane project is missing the configured state name (e.g., no
    'Testing'), pylon logs and skips that transition rather than crashing."""
    states_without_testing = [s for s in QF_STATES if s["name"] != "Testing"]
    plane = FakePlane(
        {(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-review")},
        states_by_project={PROJECT_UUID: states_without_testing, PT_UUID: states_without_testing},
    )
    event = parse_pull_request(pr_merged)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert plane.calls == []  # no update_state, no add_link, no add_comment


async def test_bundle_pr_applies_state_to_all_refs(pr_merged: dict, cfg: Config) -> None:
    # Bundle convention: refs are space-separated at the start of the title.
    pr_merged["pull_request"]["title"] = "QF-12 QF-13: bundle merge"
    plane = FakePlane(
        {
            (PROJECT_UUID, 12): _wi("wi-12", 12, "qf-review"),
            (PROJECT_UUID, 13): _wi("wi-13", 13, "qf-review"),
        }
    )
    event = parse_pull_request(pr_merged)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert ("update_state", "wi-12", "qf-testing") in plane.calls
    assert ("update_state", "wi-13", "qf-testing") in plane.calls


async def test_link_idempotent_on_replay(pr_opened: dict, cfg: Config) -> None:
    pr_opened["pull_request"]["title"] = "QF-12: replay test"
    plane = FakePlane({(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-todo")})
    resolver = Resolver(plane)
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, resolver)
    await handle_pull_request(event, cfg, plane, resolver)
    add_link_count = sum(1 for c in plane.calls if c[0] == "add_link")
    add_link_dup_count = sum(1 for c in plane.calls if c[0] == "add_link_dup")
    assert add_link_count == 1
    assert add_link_dup_count == 1


async def test_cross_project_refs_route_independently(pr_opened: dict, cfg: Config) -> None:
    pr_opened["pull_request"]["title"] = "QF-12 PT-1: cross-project bundle"
    plane = FakePlane(
        {
            (PROJECT_UUID, 12): _wi("wi-12", 12, "qf-todo"),
            (PT_UUID, 1): _wi("wi-pt-1", 1, "qf-todo"),
        }
    )
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert ("update_state", "wi-12", "qf-review") in plane.calls
    assert ("update_state", "wi-pt-1", "qf-review") in plane.calls


# ── Transient-failure propagation (regression: silent state drift) ──


async def test_failing_ref_raises_handler_error(pr_merged: dict, cfg: Config) -> None:
    """A transient Plane-API failure on update_state must NOT be
    swallowed-and-forgotten — it has to propagate so the webhook route
    can answer non-2xx and let GitHub retry. This is the root-cause
    fix: previously the per-ref try/except ate the exception and the
    merged→Done transition vanished forever."""
    plane = FakePlane({(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-review")})

    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated Plane 429 under batch-merge load")

    plane.update_state = boom  # type: ignore[method-assign]

    event = parse_pull_request(pr_merged)
    with pytest.raises(PullRequestHandlerError):
        await handle_pull_request(event, cfg, plane, Resolver(plane))


async def test_bundle_pr_applies_good_ref_then_raises_for_bad_ref(
    pr_merged: dict, cfg: Config
) -> None:
    """One bad ref shouldn't block the others — but the run must still
    end in a raise so GitHub retries the whole delivery. On replay the
    already-applied good ref is idempotent (state set-to-target)."""
    pr_merged["pull_request"]["title"] = "QF-12 QF-13: bundle merge"
    plane = FakePlane(
        {
            (PROJECT_UUID, 12): _wi("wi-12", 12, "qf-review"),
            (PROJECT_UUID, 13): _wi("wi-13", 13, "qf-review"),
        }
    )

    original = plane.update_state

    async def maybe_boom(project_id: str, work_item_id: str, state_uuid: str) -> None:
        if work_item_id == "wi-13":
            raise RuntimeError("transient Plane outage")
        await original(project_id, work_item_id, state_uuid)

    plane.update_state = maybe_boom  # type: ignore[method-assign]

    event = parse_pull_request(pr_merged)
    with pytest.raises(PullRequestHandlerError):
        await handle_pull_request(event, cfg, plane, Resolver(plane))

    # The healthy ref still landed before the raise.
    assert ("update_state", "wi-12", "qf-testing") in plane.calls


async def test_module_reconcile_failure_does_not_raise_handler_error(
    pr_opened: dict,
) -> None:
    """The flip side of the propagation fix: a module-reconcile failure
    must STILL be swallowed (the ticket-state work that matters already
    landed) — it must not trigger a spurious GitHub retry."""
    cfg = _config(modules_enabled=True)
    pr_opened["pull_request"]["title"] = "QF-12: kickoff"
    plane = FakePlane(
        work_items={
            (PROJECT_UUID, 12): _wi_with_modules("wi-12", 12, "qf-todo", ["mod-I"]),
        },
        modules={"mod-I": _module("mod-I", "planned")},
        module_items={"mod-I": [{"id": "wi-12", "state": "qf-todo"}]},
    )

    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated module outage")

    plane.update_module_status = boom  # type: ignore[method-assign]

    event = parse_pull_request(pr_opened)
    # No raise — state landed; module failure stays contained.
    await handle_pull_request(event, cfg, plane, Resolver(plane))
    assert ("update_state", "wi-12", "qf-review") in plane.calls


# ── Module-state reconciliation ────────────────────────────────────


def _wi_with_modules(
    uuid: str, seq: int, state_uuid: str, module_ids: list[str]
) -> dict[str, Any]:
    return {
        "id": uuid,
        "sequence_id": seq,
        "state": state_uuid,
        "module_ids": list(module_ids),
    }


def _module(module_id: str, status: str) -> dict[str, Any]:
    return {"id": module_id, "status": status}


async def test_module_reconcile_promotes_planned_to_in_progress(
    pr_opened: dict,
) -> None:
    """A PR opens against the only ticket in a planned module — the
    module should auto-flip to 'in-progress'."""
    cfg = _config(modules_enabled=True)
    pr_opened["pull_request"]["title"] = "QF-12: kickoff"
    plane = FakePlane(
        work_items={
            (PROJECT_UUID, 12): _wi_with_modules(
                "wi-12", 12, "qf-todo", ["mod-A"]
            ),
        },
        modules={"mod-A": _module("mod-A", "planned")},
        module_items={
            "mod-A": [
                {"id": "wi-12", "state": "qf-todo"},
                {"id": "wi-13", "state": "qf-todo"},
            ],
        },
    )
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, Resolver(plane))

    assert ("update_state", "wi-12", "qf-review") in plane.calls
    assert ("update_module_status", "mod-A", "in-progress") in plane.calls


async def test_module_reconcile_completes_when_last_ticket_done(
    pr_merged: dict,
) -> None:
    """The merging PR ticks the last running ticket in the module
    over to Done — the module should flip to 'completed'."""
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={
            (PROJECT_UUID, 12): _wi_with_modules(
                "wi-12", 12, "qf-review", ["mod-B"]
            ),
        },
        modules={"mod-B": _module("mod-B", "in-progress")},
        module_items={
            "mod-B": [
                # wi-12 will be flipped to qf-testing by the merge
                # transition. The fake reflects the state change in
                # update_state so the post-transition reconcile sees
                # the new value.
                {"id": "wi-12", "state": "qf-review"},
                {"id": "wi-99", "state": "qf-done"},  # already done
            ],
        },
    )
    # Make `merged` map to Done in this test (the canonical "all
    # items terminal" state) — the default _config uses Testing,
    # which is a *started* state so the module would NOT complete.
    cfg.state_machine.merged = "Done"

    event = parse_pull_request(pr_merged)
    await handle_pull_request(event, cfg, plane, Resolver(plane))

    assert ("update_state", "wi-12", "qf-done") in plane.calls
    assert ("update_module_status", "mod-B", "completed") in plane.calls


async def test_module_reconcile_does_not_re_promote_in_progress_module(
    pr_opened: dict,
) -> None:
    cfg = _config(modules_enabled=True)
    pr_opened["pull_request"]["title"] = "QF-12: another ticket"
    plane = FakePlane(
        work_items={
            (PROJECT_UUID, 12): _wi_with_modules(
                "wi-12", 12, "qf-todo", ["mod-C"]
            ),
        },
        modules={"mod-C": _module("mod-C", "in-progress")},
        module_items={
            "mod-C": [
                {"id": "wi-12", "state": "qf-todo"},
                {"id": "wi-77", "state": "qf-progress"},
            ],
        },
    )
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, Resolver(plane))

    update_calls = [c for c in plane.calls if c[0] == "update_module_status"]
    assert update_calls == []


async def test_module_reconcile_skipped_when_disabled(pr_opened: dict) -> None:
    """The default opt-out: cfg.modules.enabled = False suppresses
    every module API call regardless of what happens to the ticket."""
    cfg = _config(modules_enabled=False)
    pr_opened["pull_request"]["title"] = "QF-12: kickoff"
    plane = FakePlane(
        work_items={
            (PROJECT_UUID, 12): _wi_with_modules(
                "wi-12", 12, "qf-todo", ["mod-D"]
            ),
        },
        modules={"mod-D": _module("mod-D", "planned")},
        module_items={
            "mod-D": [{"id": "wi-12", "state": "qf-todo"}],
        },
    )
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, Resolver(plane))

    # State transition fires; module reconcile does not.
    assert ("update_state", "wi-12", "qf-review") in plane.calls
    assert all(c[0] != "get_module" for c in plane.calls)
    assert all(c[0] != "update_module_status" for c in plane.calls)


async def test_module_reconcile_skipped_when_ticket_state_unchanged(
    pr_closed_unmerged: dict,
) -> None:
    """closed_unmerged from a state that doesn't match `if_state`
    posts a comment but doesn't transition the ticket — so the
    module reconcile shouldn't fire either."""
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={
            (PROJECT_UUID, 13): _wi_with_modules(
                "wi-13", 13, "qf-progress", ["mod-E"]
            ),
        },
        modules={"mod-E": _module("mod-E", "planned")},
        module_items={
            "mod-E": [{"id": "wi-13", "state": "qf-progress"}],
        },
    )
    event = parse_pull_request(pr_closed_unmerged)
    await handle_pull_request(event, cfg, plane, Resolver(plane))

    # No state transition (current state != if_state); no module call.
    assert all(c[0] != "update_state" for c in plane.calls)
    assert all(c[0] != "get_module" for c in plane.calls)
    assert any(c[0] == "add_comment" for c in plane.calls)


async def test_module_reconcile_handles_multiple_modules(
    pr_opened: dict,
) -> None:
    """A ticket in N modules reconciles each one independently."""
    cfg = _config(modules_enabled=True)
    pr_opened["pull_request"]["title"] = "QF-12: shared work"
    plane = FakePlane(
        work_items={
            (PROJECT_UUID, 12): _wi_with_modules(
                "wi-12", 12, "qf-todo", ["mod-F", "mod-G"]
            ),
        },
        modules={
            "mod-F": _module("mod-F", "planned"),
            "mod-G": _module("mod-G", "backlog"),  # also promoted
        },
        module_items={
            "mod-F": [{"id": "wi-12", "state": "qf-todo"}],
            "mod-G": [{"id": "wi-12", "state": "qf-todo"}],
        },
    )
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, Resolver(plane))

    assert ("update_module_status", "mod-F", "in-progress") in plane.calls
    assert ("update_module_status", "mod-G", "in-progress") in plane.calls


async def test_module_reconcile_skips_locked_paused_module(
    pr_opened: dict,
) -> None:
    cfg = _config(modules_enabled=True)
    pr_opened["pull_request"]["title"] = "QF-12: kickoff"
    plane = FakePlane(
        work_items={
            (PROJECT_UUID, 12): _wi_with_modules(
                "wi-12", 12, "qf-todo", ["mod-H"]
            ),
        },
        modules={"mod-H": _module("mod-H", "paused")},
        module_items={
            "mod-H": [{"id": "wi-12", "state": "qf-todo"}],
        },
    )
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, Resolver(plane))

    # State transition still happens; module is locked, so no patch.
    assert ("update_state", "wi-12", "qf-review") in plane.calls
    update_calls = [c for c in plane.calls if c[0] == "update_module_status"]
    assert update_calls == []


async def test_module_reconcile_swallows_module_api_errors(
    pr_opened: dict,
) -> None:
    """A failing module reconcile must not propagate to the GitHub
    webhook caller — the ticket-state work is what matters and has
    already happened."""
    cfg = _config(modules_enabled=True)
    pr_opened["pull_request"]["title"] = "QF-12: kickoff"
    plane = FakePlane(
        work_items={
            (PROJECT_UUID, 12): _wi_with_modules(
                "wi-12", 12, "qf-todo", ["mod-I"]
            ),
        },
        modules={"mod-I": _module("mod-I", "planned")},
        module_items={"mod-I": [{"id": "wi-12", "state": "qf-todo"}]},
    )

    # Sabotage update_module_status so the reconcile branch raises.
    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated module outage")

    plane.update_module_status = boom  # type: ignore[method-assign]

    event = parse_pull_request(pr_opened)
    # No raise — handler swallows the module-reconcile failure.
    await handle_pull_request(event, cfg, plane, Resolver(plane))

    assert ("update_state", "wi-12", "qf-review") in plane.calls


async def test_module_reconcile_skips_when_module_lookup_returns_none(
    pr_opened: dict,
) -> None:
    """If the module_id on the ticket points at a module that's been
    deleted out-of-band, the reconciler logs and moves on without
    erroring."""
    cfg = _config(modules_enabled=True)
    pr_opened["pull_request"]["title"] = "QF-12: kickoff"
    plane = FakePlane(
        work_items={
            (PROJECT_UUID, 12): _wi_with_modules(
                "wi-12", 12, "qf-todo", ["mod-ghost"]
            ),
        },
        # Empty modules dict → get_module("mod-ghost") returns None.
    )
    event = parse_pull_request(pr_opened)
    await handle_pull_request(event, cfg, plane, Resolver(plane))

    assert ("update_state", "wi-12", "qf-review") in plane.calls
    update_calls = [c for c in plane.calls if c[0] == "update_module_status"]
    assert update_calls == []


# ── Plane-event entry point ────────────────────────────────────────


def _issue_updated_payload(
    *,
    work_item_id: str,
    project_id: str = PROJECT_UUID,
    module_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "event": "issue",
        "action": "updated",
        "data": {
            "id": work_item_id,
            "project_id": project_id,
            "module_ids": module_ids or [],
        },
    }


async def test_plane_event_reconciles_planned_module_to_in_progress() -> None:
    """Plane fires `issue.updated` after a state change from any source
    (UI, MCP, API). pylon should reconcile the modules just like the
    GitHub-PR path does."""
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-progress")},
        modules={"mod-A": _module("mod-A", "planned")},
        module_items={
            "mod-A": [{"id": "wi-12", "state": "qf-progress"}],
        },
    )
    event = parse_plane_event(
        _issue_updated_payload(work_item_id="wi-12", module_ids=["mod-A"])
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    assert ("update_module_status", "mod-A", "in-progress") in plane.calls


async def test_plane_event_completes_module_when_all_items_terminal() -> None:
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={(PROJECT_UUID, 99): _wi("wi-99", 99, "qf-done")},
        modules={"mod-B": _module("mod-B", "in-progress")},
        module_items={
            "mod-B": [
                {"id": "wi-99", "state": "qf-done"},
                {"id": "wi-100", "state": "qf-cancelled"},
            ],
        },
    )
    event = parse_plane_event(
        _issue_updated_payload(work_item_id="wi-99", module_ids=["mod-B"])
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    assert ("update_module_status", "mod-B", "completed") in plane.calls


async def test_plane_event_does_not_re_promote_in_progress_module() -> None:
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-progress")},
        modules={"mod-C": _module("mod-C", "in-progress")},
        module_items={"mod-C": [{"id": "wi-12", "state": "qf-progress"}]},
    )
    event = parse_plane_event(
        _issue_updated_payload(work_item_id="wi-12", module_ids=["mod-C"])
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    assert all(c[0] != "update_module_status" for c in plane.calls)


async def test_plane_event_ignored_when_action_not_actionable() -> None:
    cfg = _config(modules_enabled=True)
    plane = FakePlane(work_items={})
    event = parse_plane_event(
        {"event": "issue", "action": "deleted", "data": {"id": "x"}}
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    assert plane.calls == []


async def test_plane_event_skipped_when_modules_disabled() -> None:
    cfg = _config(modules_enabled=False)
    plane = FakePlane(
        work_items={},
        modules={"mod-D": _module("mod-D", "planned")},
        module_items={
            "mod-D": [{"id": "wi-1", "state": "qf-progress"}],
        },
    )
    event = parse_plane_event(
        _issue_updated_payload(work_item_id="wi-1", module_ids=["mod-D"])
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    assert plane.calls == []


async def test_plane_event_enumerates_modules_when_payload_omits_module_ids() -> None:
    """Plane CE's `issue.updated` payload omits module_ids on state
    changes AND the external issue endpoint omits the field too —
    so pylon enumerates the project's modules and checks each one's
    work-item list for our ticket. The matching module(s) reconcile.

    This is the M6 reopen case from operator report:
    Completed module + a reopened ticket → flip back to in-progress."""
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={},
        modules={
            "mod-M6": _module("mod-M6", "completed"),
            "mod-other": _module("mod-other", "in-progress"),
        },
        module_items={
            # M6 contains our reopened ticket + a done sibling.
            "mod-M6": [
                {"id": "wi-12", "state": "qf-progress"},
                {"id": "wi-99", "state": "qf-done"},
            ],
            # Unrelated module — should be enumerated (membership
            # check) but never get an update_module_status call.
            "mod-other": [{"id": "wi-xyz", "state": "qf-progress"}],
        },
    )
    event = parse_plane_event(
        _issue_updated_payload(work_item_id="wi-12", module_ids=[])
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    assert ("list_modules", PROJECT_UUID) in plane.calls
    assert ("list_module_work_items", "mod-M6") in plane.calls
    assert ("list_module_work_items", "mod-other") in plane.calls
    # Only M6 actually contains wi-12, so only M6 updates.
    assert ("update_module_status", "mod-M6", "in-progress") in plane.calls
    assert not any(
        c[0] == "update_module_status" and c[1] == "mod-other"
        for c in plane.calls
    )


async def test_plane_event_payload_module_ids_short_circuit_enumeration() -> None:
    """When module_ids ARE in the payload, pylon trusts them and
    skips the expensive list_modules + per-module work-item fetch.

    Forward-compatible: when the upstream Plane patch (PU-N) lands,
    payloads will start carrying module_ids and pylon automatically
    drops to this cheaper path with no code change."""
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={},
        modules={
            "mod-A": _module("mod-A", "planned"),
            # An unrelated module: must never be touched.
            "mod-other": _module("mod-other", "planned"),
        },
        module_items={
            "mod-A": [{"id": "wi-12", "state": "qf-progress"}],
            "mod-other": [{"id": "wi-other", "state": "qf-progress"}],
        },
    )
    event = parse_plane_event(
        _issue_updated_payload(work_item_id="wi-12", module_ids=["mod-A"])
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    assert ("update_module_status", "mod-A", "in-progress") in plane.calls
    # No enumeration when payload has the answer.
    assert not any(c[0] == "list_modules" for c in plane.calls)
    assert not any(
        c[0] == "list_module_work_items" and c[1] == "mod-other"
        for c in plane.calls
    )


async def test_plane_event_fallback_skipped_when_work_item_id_missing() -> None:
    """If we can't identify the work item (no id in payload), there's
    no membership query we can issue. Skip the enumeration."""
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={},
        modules={"mod-A": _module("mod-A", "planned")},
        module_items={"mod-A": [{"id": "wi-other", "state": "qf-progress"}]},
    )
    event = parse_plane_event(
        {
            "event": "issue",
            "action": "updated",
            "data": {"project_id": PROJECT_UUID, "module_ids": []},
        }
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    assert not any(c[0] == "list_modules" for c in plane.calls)
    assert not any(c[0] == "update_module_status" for c in plane.calls)


async def test_plane_event_fallback_when_ticket_belongs_to_no_module() -> None:
    """An issue.updated for a ticket that's not in any module:
    enumeration runs but no module matches. No reconcile attempts."""
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={},
        modules={"mod-A": _module("mod-A", "planned")},
        module_items={"mod-A": [{"id": "wi-other", "state": "qf-progress"}]},
    )
    event = parse_plane_event(
        _issue_updated_payload(work_item_id="wi-not-in-any-module", module_ids=[])
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    assert ("list_modules", PROJECT_UUID) in plane.calls
    assert ("list_module_work_items", "mod-A") in plane.calls
    assert not any(c[0] == "update_module_status" for c in plane.calls)


async def test_plane_event_fallback_handles_list_modules_error() -> None:
    """If list_modules itself fails (Plane API hiccup), log and skip
    — don't propagate the exception out of the webhook handler."""
    cfg = _config(modules_enabled=True)

    class BrokenPlane(FakePlane):
        async def list_modules(self, project_id: str) -> list[dict]:
            self.calls.append(("list_modules", project_id))
            raise RuntimeError("plane API exploded")

    plane = BrokenPlane(work_items={})
    event = parse_plane_event(
        _issue_updated_payload(work_item_id="wi-12", module_ids=[])
    )
    # Must not raise.
    await handle_plane_event(event, cfg, plane, Resolver(plane))
    assert not any(c[0] == "update_module_status" for c in plane.calls)


async def test_plane_event_fallback_continues_when_one_module_list_fails() -> None:
    """One bad list_module_work_items shouldn't block the rest of
    the enumeration — pylon soldiers on to find the matching module."""
    cfg = _config(modules_enabled=True)

    class FlakyPlane(FakePlane):
        async def list_module_work_items(
            self, project_id: str, module_id: str
        ) -> list[dict]:
            self.calls.append(("list_module_work_items", module_id))
            if module_id == "mod-broken":
                raise RuntimeError("transient")
            return list(self.module_items.get(module_id, []))

    plane = FlakyPlane(
        work_items={},
        modules={
            "mod-broken": _module("mod-broken", "in-progress"),
            "mod-good": _module("mod-good", "completed"),
        },
        module_items={
            "mod-good": [{"id": "wi-12", "state": "qf-progress"}],
        },
    )
    event = parse_plane_event(
        _issue_updated_payload(work_item_id="wi-12", module_ids=[])
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    # The good module still reconciled despite mod-broken raising.
    assert ("update_module_status", "mod-good", "in-progress") in plane.calls


async def test_plane_event_skipped_when_project_id_missing() -> None:
    """A malformed event without project_id — log and skip."""
    cfg = _config(modules_enabled=True)
    plane = FakePlane(work_items={})
    event = parse_plane_event(
        {
            "event": "issue",
            "action": "updated",
            "data": {"id": "wi-12", "module_ids": ["mod-X"]},
            # no project_id anywhere
        }
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    assert plane.calls == []


async def test_plane_event_reconciles_each_module_independently() -> None:
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-progress")},
        modules={
            "mod-E": _module("mod-E", "planned"),
            "mod-F": _module("mod-F", "backlog"),
        },
        module_items={
            "mod-E": [{"id": "wi-12", "state": "qf-progress"}],
            "mod-F": [{"id": "wi-12", "state": "qf-progress"}],
        },
    )
    event = parse_plane_event(
        _issue_updated_payload(
            work_item_id="wi-12", module_ids=["mod-E", "mod-F"]
        )
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    assert ("update_module_status", "mod-E", "in-progress") in plane.calls
    assert ("update_module_status", "mod-F", "in-progress") in plane.calls


async def test_plane_event_one_failing_module_does_not_block_others() -> None:
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-progress")},
        modules={
            "mod-good": _module("mod-good", "planned"),
            "mod-bad": _module("mod-bad", "planned"),
        },
        module_items={
            "mod-good": [{"id": "wi-12", "state": "qf-progress"}],
            "mod-bad": [{"id": "wi-12", "state": "qf-progress"}],
        },
    )

    # Sabotage update_module_status to raise only for `mod-bad`.
    original = plane.update_module_status

    async def maybe_boom(project_id: str, module_id: str, status: str) -> None:
        if module_id == "mod-bad":
            raise RuntimeError("simulated module outage")
        await original(project_id, module_id, status)

    plane.update_module_status = maybe_boom  # type: ignore[method-assign]

    event = parse_plane_event(
        _issue_updated_payload(
            work_item_id="wi-12", module_ids=["mod-good", "mod-bad"]
        )
    )
    # Must not raise — the good module still gets reconciled.
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    # The successful reconcile recorded; the bad one didn't because
    # the maybe_boom wrapper raised before original() could record.
    assert any(
        c == ("update_module_status", "mod-good", "in-progress")
        for c in plane.calls
    )


async def test_plane_event_idempotent_against_pylons_own_changes() -> None:
    """When pylon's GitHub handler updates a ticket state, Plane fires
    issue.updated for the same change. pylon's Plane handler then sees
    a module whose ``current_status`` already matches the desired
    one — and no-ops. This is the feedback-loop guard."""
    cfg = _config(modules_enabled=True)
    plane = FakePlane(
        work_items={(PROJECT_UUID, 12): _wi("wi-12", 12, "qf-progress")},
        # Module already at the status the reconciler would compute —
        # i.e. pylon already flipped it from the GitHub path moments
        # ago, and now Plane is replaying via webhook.
        modules={"mod-X": _module("mod-X", "in-progress")},
        module_items={"mod-X": [{"id": "wi-12", "state": "qf-progress"}]},
    )
    event = parse_plane_event(
        _issue_updated_payload(work_item_id="wi-12", module_ids=["mod-X"])
    )
    await handle_plane_event(event, cfg, plane, Resolver(plane))

    # get_module + list_module_work_items fire (we have to read to
    # know it's a no-op), but no update_module_status call.
    assert ("get_module", "mod-X") in plane.calls
    assert all(c[0] != "update_module_status" for c in plane.calls)
