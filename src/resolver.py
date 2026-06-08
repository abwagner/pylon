"""Caches Plane project + state lookups so the handler doesn't repeatedly
hit the API for things that only change when the user reorganizes Plane.

Cache lifetime is the lifetime of the pylon process. To pick up project
renames or new states, restart the container. This is a deliberate choice:
near-zero-staleness isn't worth the complexity of TTLs and invalidation,
and `docker compose restart pylon` is one command.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol


logger = logging.getLogger(__name__)


class _PlaneAPI(Protocol):
    async def list_projects(self) -> list[dict]: ...
    async def list_states(self, project_id: str) -> list[dict]: ...


class ProjectStates:
    """Bidirectional name ↔ uuid map for a single project's states,
    plus the state's `group` field so the module-state reconciler
    can ask 'is this work item in the started / completed / cancelled
    group?' without re-reading the state list.

    Plane's state groups are stable across CE: ``backlog``,
    ``unstarted``, ``started``, ``completed``, ``cancelled``.
    """

    def __init__(self, raw_states: list[dict]):
        self.by_name_lower: dict[str, str] = {}
        self.by_uuid_to_name: dict[str, str] = {}
        self.group_by_uuid: dict[str, str] = {}
        for s in raw_states:
            uuid = s.get("id")
            name = (s.get("name") or "").strip()
            group = (s.get("group") or "").strip()
            if not uuid or not name:
                continue
            self.by_name_lower[name.lower()] = uuid
            self.by_uuid_to_name[uuid] = name
            if group:
                self.group_by_uuid[uuid] = group

    def uuid_for(self, state_name: str) -> str | None:
        return self.by_name_lower.get(state_name.strip().lower())

    def name_for(self, state_uuid: str | None) -> str | None:
        if not state_uuid:
            return None
        return self.by_uuid_to_name.get(state_uuid)

    def group_for(self, state_uuid: str | None) -> str | None:
        if not state_uuid:
            return None
        return self.group_by_uuid.get(state_uuid)


class Resolver:
    """Lazily caches project + state lookups, shared across all
    concurrent webhook handlers.

    Cache population is guarded by ``asyncio.Lock``s using the
    double-checked pattern. Without them, a batch-merge that fans out
    many webhooks against a cold cache had every concurrent first-hit
    re-fetch ``list_projects`` / ``list_states`` simultaneously — a
    self-inflicted burst on the same Plane API the handler is trying
    not to overrun. The lock collapses that to one fetch; everyone
    else awaits it and reads the populated cache.
    """

    def __init__(self, plane: _PlaneAPI):
        self._plane = plane
        self._projects: dict[str, str] | None = None
        self._states: dict[str, ProjectStates] = {}
        self._projects_lock = asyncio.Lock()
        # One lock per project for state population, created on demand
        # under _state_locks_guard so distinct projects don't serialize
        # against each other.
        self._state_locks: dict[str, asyncio.Lock] = {}
        self._state_locks_guard = asyncio.Lock()

    async def project_uuid(self, identifier: str) -> str | None:
        if self._projects is None:
            async with self._projects_lock:
                # Re-check: another coroutine may have populated the
                # cache while we waited for the lock.
                if self._projects is None:
                    raw = await self._plane.list_projects()
                    self._projects = {
                        p["identifier"]: p["id"]
                        for p in raw
                        if p.get("identifier") and p.get("id")
                    }
                    logger.info(
                        "loaded %d projects: %s",
                        len(self._projects),
                        sorted(self._projects),
                    )
        return self._projects.get(identifier)

    async def states(self, project_uuid: str) -> ProjectStates:
        cached = self._states.get(project_uuid)
        if cached is not None:
            return cached
        lock = await self._state_lock_for(project_uuid)
        async with lock:
            cached = self._states.get(project_uuid)
            if cached is not None:
                return cached
            raw = await self._plane.list_states(project_uuid)
            states = ProjectStates(raw)
            self._states[project_uuid] = states
            logger.info(
                "loaded %d states for project %s: %s",
                len(states.by_uuid_to_name),
                project_uuid,
                sorted(states.by_uuid_to_name.values()),
            )
            return states

    async def _state_lock_for(self, project_uuid: str) -> asyncio.Lock:
        async with self._state_locks_guard:
            lock = self._state_locks.get(project_uuid)
            if lock is None:
                lock = asyncio.Lock()
                self._state_locks[project_uuid] = lock
            return lock
