"""Translate external events (GitHub PRs, Plane state changes) into
Plane API mutations.

Two entry points:

* :func:`handle_pull_request` — applies the PR-driven state machine
  documented in the module docstring below and reconciles modules
  on each successful ticket-state transition.
* :func:`handle_plane_event` — runs the module reconciler in
  response to a Plane-emitted ``issue.updated`` event. Catches state
  changes pylon didn't drive (manual UI edits, MCP / REST calls,
  upstream automations) so module status stays consistent with all
  ticket movement, not just PR-driven movement.

State machine (PR path), configured via config.yaml's
``state_machine:`` block:

  opened_draft        → state = state_machine.opened_draft  + attach link
  opened_ready        → state = state_machine.opened_ready  + attach link
  ready_for_review    → state = state_machine.ready_for_review + attach link
  converted_to_draft  → state = state_machine.converted_to_draft (no link op)
  merged              → state = state_machine.merged
  closed_unmerged     → if current state == closed_unmerged.if_state
                          then state = closed_unmerged.set_to
                        always: post a comment with the PR URL
"""

from __future__ import annotations

import logging
from typing import Protocol

from .config import Config
from .github_events import ParsedPullRequest, PRRef
from .module_state import desired_module_status
from .plane_events import ParsedPlaneEvent
from .resolver import ProjectStates, Resolver


logger = logging.getLogger(__name__)


class PlaneAPI(Protocol):
    async def get_work_item_by_sequence(
        self, project_id: str, sequence: int
    ) -> dict | None: ...
    async def update_state(
        self, project_id: str, work_item_id: str, state_uuid: str
    ) -> None: ...
    async def add_link(
        self, project_id: str, work_item_id: str, link_url: str, title: str = ""
    ) -> bool: ...
    async def add_comment(
        self, project_id: str, work_item_id: str, comment_html: str
    ) -> None: ...
    # ── Module API surface (used by the module-state reconciler) ───
    async def get_module(self, project_id: str, module_id: str) -> dict | None: ...
    async def list_modules(self, project_id: str) -> list[dict]: ...
    async def list_module_work_items(
        self, project_id: str, module_id: str
    ) -> list[dict]: ...
    async def update_module_status(
        self, project_id: str, module_id: str, status: str
    ) -> None: ...


async def handle_pull_request(
    event: ParsedPullRequest, cfg: Config, plane: PlaneAPI, resolver: Resolver
) -> None:
    if event.action == "ignored":
        return
    if not event.refs:
        logger.info("PR #%s has no XX-N refs; nothing to do", event.number)
        return

    for ref in event.refs:
        try:
            await _apply_ref(event, ref, cfg, plane, resolver)
        except Exception:
            # One bad ref shouldn't stop us from processing the others.
            logger.exception("handler error processing ref %s in PR #%s", ref, event.number)


async def _apply_ref(
    event: ParsedPullRequest,
    ref: PRRef,
    cfg: Config,
    plane: PlaneAPI,
    resolver: Resolver,
) -> None:
    project_uuid = await resolver.project_uuid(ref.project)
    if not project_uuid:
        logger.warning("ref %s: project not found in Plane workspace", ref)
        return

    work_item = await plane.get_work_item_by_sequence(project_uuid, ref.sequence)
    if not work_item:
        logger.warning("ref %s: no Plane work item with that sequence", ref)
        return
    work_item_id: str = work_item["id"]
    states = await resolver.states(project_uuid)

    sm = cfg.state_machine
    a = event.action
    state_changed = False

    if a == "opened_draft":
        state_changed = await _set_state(
            plane, project_uuid, work_item_id, sm.opened_draft, states, ref
        )
        await _attach_link(plane, project_uuid, work_item_id, event)
    elif a == "opened_ready":
        state_changed = await _set_state(
            plane, project_uuid, work_item_id, sm.opened_ready, states, ref
        )
        await _attach_link(plane, project_uuid, work_item_id, event)
    elif a == "ready_for_review":
        state_changed = await _set_state(
            plane, project_uuid, work_item_id, sm.ready_for_review, states, ref
        )
        await _attach_link(plane, project_uuid, work_item_id, event)
    elif a == "converted_to_draft":
        state_changed = await _set_state(
            plane, project_uuid, work_item_id, sm.converted_to_draft, states, ref
        )
    elif a == "merged":
        state_changed = await _set_state(
            plane, project_uuid, work_item_id, sm.merged, states, ref
        )
    elif a == "closed_unmerged":
        current_name = states.name_for(work_item.get("state"))
        rule = sm.closed_unmerged
        if current_name and current_name.strip().lower() == rule.if_state.strip().lower():
            state_changed = await _set_state(
                plane, project_uuid, work_item_id, rule.set_to, states, ref
            )
        else:
            logger.info(
                "ref %s: PR closed without merging; current state %r ≠ %r, leaving state alone",
                ref,
                current_name,
                rule.if_state,
            )
        comment = (
            f'<p>PR #{event.number} closed without merging — '
            f'<a href="{event.url}">{event.url}</a></p>'
        )
        await plane.add_comment(project_uuid, work_item_id, comment)

    # ── Module reconciliation ──────────────────────────────────────
    # When pylon flips a ticket's state, the modules it belongs to may
    # need a status update too. Gated on cfg.modules.enabled so
    # operators can opt out without touching the handler.
    if state_changed and cfg.modules.enabled:
        module_ids = _module_ids(work_item)
        for module_id in module_ids:
            try:
                await _reconcile_module(
                    plane, project_uuid, module_id, states, log_tag=str(ref)
                )
            except Exception:
                # One bad module shouldn't block the others, and a
                # module-reconcile failure must never propagate to the
                # GitHub webhook caller (we already did the work that
                # matters — the ticket state).
                logger.exception(
                    "ref %s: module reconcile failed for module %s",
                    ref,
                    module_id,
                )


async def _set_state(
    plane: PlaneAPI,
    project_uuid: str,
    work_item_id: str,
    state_name: str,
    states: ProjectStates,
    ref: PRRef,
) -> bool:
    """Update the work-item state. Returns True iff the API call landed
    (caller uses this to decide whether to reconcile modules)."""
    target = states.uuid_for(state_name)
    if not target:
        logger.warning(
            "ref %s: target state %r not found in project; available: %s",
            ref,
            state_name,
            sorted(states.by_uuid_to_name.values()),
        )
        return False
    await plane.update_state(project_uuid, work_item_id, target)
    logger.info("ref %s: state → %r", ref, state_name)
    return True


async def _attach_link(
    plane: PlaneAPI, project_uuid: str, work_item_id: str, event: ParsedPullRequest
) -> None:
    created = await plane.add_link(project_uuid, work_item_id, event.url, event.title)
    logger.info(
        "PR #%s link %s on %s",
        event.number,
        "attached" if created else "already present",
        work_item_id,
    )


# ── Module reconciliation ──────────────────────────────────────────


def _module_ids(work_item: dict) -> list[str]:
    """Extract the modules a work item belongs to from Plane's payload.

    Plane returns the list under ``module_ids`` (the public REST
    shape). We coerce defensively because the field is occasionally
    absent or ``None`` on older work-item records.
    """
    raw = work_item.get("module_ids")
    if not isinstance(raw, list):
        return []
    return [m for m in raw if isinstance(m, str) and m]


async def _reconcile_module(
    plane: PlaneAPI,
    project_uuid: str,
    module_id: str,
    states: ProjectStates,
    *,
    log_tag: str,
    module: dict | None = None,
    items: list[dict] | None = None,
) -> None:
    """Decide whether ``module_id``'s ``status`` should change in
    response to a work-item state change, and apply the change if
    so. ``log_tag`` is a short caller-supplied label (the GitHub
    handler passes the PR ref; the Plane handler passes the work-
    item id) so log lines from either entry point are correlatable
    back to the trigger.

    ``module`` and ``items`` short-circuit the redundant fetches when
    the caller already pulled them (e.g. the Plane webhook fallback
    that enumerates modules and checks membership before reconciling
    — it already has both records).
    """
    if module is None:
        module = await plane.get_module(project_uuid, module_id)
    if module is None:
        logger.info(
            "%s: module %s not found; skipping reconcile", log_tag, module_id
        )
        return
    current = (module.get("status") or "").strip().lower() or None
    if items is None:
        items = await plane.list_module_work_items(project_uuid, module_id)
    state_uuids: list[str | None] = []
    for it in items:
        s = it.get("state")
        state_uuids.append(s if isinstance(s, str) and s else None)
    new_status = desired_module_status(
        item_state_uuids=state_uuids,
        state_to_group=states.group_by_uuid,
        current_status=current,
    )
    if new_status is None or new_status == current:
        logger.debug(
            "%s: module %s stays at %r (items=%d)",
            log_tag,
            module_id,
            current,
            len(items),
        )
        return
    await plane.update_module_status(project_uuid, module_id, new_status)
    logger.info(
        "%s: module %s status %r → %r (items=%d)",
        log_tag,
        module_id,
        current,
        new_status,
        len(items),
    )


# ── Plane-event entry point ────────────────────────────────────────


async def handle_plane_event(
    event: ParsedPlaneEvent, cfg: Config, plane: PlaneAPI, resolver: Resolver
) -> None:
    """Reconcile every module a Plane-event-touched work item belongs
    to. The trigger is Plane's own ``issue.updated`` webhook, so this
    fires regardless of whether the state change came from a PR (in
    which case the GitHub handler already reconciled), from the
    Plane UI, from an MCP call, or from any other source.

    ``cfg.modules.enabled = False`` makes this a no-op so operators
    can flip the whole reconciliation feature off without touching
    the webhook subscription.
    """
    if event.action == "ignored":
        logger.debug(
            "plane event ignored event=%r action=%r",
            event.raw_event,
            event.raw_action,
        )
        return
    if not cfg.modules.enabled:
        logger.debug("plane event: modules.enabled is false, skipping reconcile")
        return
    if not event.project_id:
        logger.info("plane event: no project_id in payload; skipping")
        return
    states = await resolver.states(event.project_id)
    log_tag = f"plane:wi={event.work_item_id or '<unknown>'}"

    if event.module_ids:
        # Trust the payload when it actually has module_ids — saves
        # the enumeration cost. This branch fires for any future
        # Plane release that ships the upstream serializer fix (PU-N
        # — adding module_ids to apps/api/plane/api/serializers/issue.py).
        for module_id in event.module_ids:
            try:
                await _reconcile_module(
                    plane, event.project_id, module_id, states, log_tag=log_tag
                )
            except Exception:
                logger.exception(
                    "%s: module reconcile failed for module %s", log_tag, module_id
                )
        return

    if not event.work_item_id:
        logger.debug(
            "plane event: no module_ids and no work_item_id; nothing to reconcile"
        )
        return

    # Plane CE's `issue.updated` payload empirically omits
    # ``module_ids`` on state-change events. The external REST API
    # also omits module linkage from the issue resource (see
    # PlaneClient.list_modules docstring), so we can't recover the
    # missing field from a work-item lookup either. Enumerate every
    # module in the project, list its work items, and reconcile each
    # module that contains the changed ticket.
    try:
        modules = await plane.list_modules(event.project_id)
    except Exception:
        logger.exception(
            "plane event: list_modules failed for project=%s wi=%s",
            event.project_id,
            event.work_item_id,
        )
        return

    matched = 0
    for module in modules:
        module_id = module.get("id")
        if not isinstance(module_id, str) or not module_id:
            continue
        try:
            items = await plane.list_module_work_items(event.project_id, module_id)
        except Exception:
            logger.exception(
                "%s: list_module_work_items failed for module %s during fallback",
                log_tag,
                module_id,
            )
            continue
        if not any(it.get("id") == event.work_item_id for it in items):
            continue
        matched += 1
        try:
            await _reconcile_module(
                plane,
                event.project_id,
                module_id,
                states,
                log_tag=log_tag,
                module=module,
                items=items,
            )
        except Exception:
            logger.exception(
                "%s: module reconcile failed for module %s", log_tag, module_id
            )
    logger.info(
        "plane event: wi=%s reconciled %d / %d modules via fallback enumeration",
        event.work_item_id,
        matched,
        len(modules),
    )
