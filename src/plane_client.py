"""Thin async wrapper around Plane's REST API.

Plane's published REST surface is documented at https://developers.plane.so/api-reference/
but a few endpoints (notably the by-identifier lookup) are not stable enough to rely on,
so this module sticks to the well-documented endpoints and resolves identifiers
via paginated list scans. Performance is fine: each project has tens of items,
each webhook fires a handful of lookups, and Plane caches its own list responses.
"""

import asyncio
import logging
import random
from typing import Any

import httpx


logger = logging.getLogger(__name__)


# HTTP status codes worth retrying: 429 (rate limited) plus the 5xx
# family (transient server-side faults). A self-hosted Plane CE under a
# rapid batch-merge burst emits exactly these when its API saturates —
# the failure mode that previously dropped ticket transitions for good.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Exponential backoff with full jitter. Defaults are deliberately
# modest — pylon runs inside a synchronous-feeling webhook handler, so
# total worst-case wait stays a few seconds. GitHub's own delivery
# retry is the outer safety net beyond this.
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 0.5  # seconds
_BACKOFF_CAP = 8.0  # seconds


class PlaneClient:
    def __init__(self, base_url: str, workspace_slug: str, api_key: str, *, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.workspace = workspace_slug
        self._client = httpx.AsyncClient(
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PlaneClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _workspace_url(self) -> str:
        return f"{self.base}/api/v1/workspaces/{self.workspace}"

    def _project_url(self, project_id: str) -> str:
        return f"{self._workspace_url()}/projects/{project_id}"

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Issue a request, retrying transient failures (429 / 5xx /
        network timeouts) with jittered exponential backoff.

        Returns the final :class:`httpx.Response` without raising for
        status — the caller still runs it through :func:`_raise_with_body`
        so a non-retryable 4xx (or an exhausted-retry 5xx) surfaces with
        the Plane response body in the log. Only the network-error path
        re-raises directly, because there's no response to hand back.
        """
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == _MAX_ATTEMPTS:
                    logger.error(
                        "Plane API %s %s failed after %d attempts: %r",
                        method, url, attempt, exc,
                    )
                    raise
                delay = _backoff_delay(attempt)
                logger.warning(
                    "Plane API %s %s network error (attempt %d/%d): %r; retrying in %.2fs",
                    method, url, attempt, _MAX_ATTEMPTS, exc, delay,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                delay = _retry_after(resp) or _backoff_delay(attempt)
                logger.warning(
                    "Plane API %s %s -> %d (attempt %d/%d); retrying in %.2fs",
                    method, url, resp.status_code, attempt, _MAX_ATTEMPTS, delay,
                )
                await asyncio.sleep(delay)
                continue

            return resp

        # Unreachable: the loop either returns a response or raises.
        assert last_exc is not None
        raise last_exc

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        resp = await self._send("GET", url, **kwargs)
        _raise_with_body(resp, "GET", url)
        return resp

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        resp = await self._send("POST", url, **kwargs)
        _raise_with_body(resp, "POST", url)
        return resp

    async def _patch(self, url: str, **kwargs) -> httpx.Response:
        resp = await self._send("PATCH", url, **kwargs)
        _raise_with_body(resp, "PATCH", url)
        return resp

    async def list_projects(self) -> list[dict]:
        url = f"{self._workspace_url()}/projects/"
        resp = await self._get(url)
        results, _ = _unwrap_paginated(resp.json())
        return results

    async def get_work_item_by_sequence(
        self, project_id: str, sequence: int
    ) -> dict | None:
        """Return the full work-item dict whose sequence_id matches, or None."""
        url = f"{self._project_url(project_id)}/issues/"
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"per_page": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await self._get(url, params=params)
            results, cursor = _unwrap_paginated(resp.json())
            for item in results:
                if item.get("sequence_id") == sequence:
                    return item
            if not cursor:
                return None

    async def update_state(
        self, project_id: str, work_item_id: str, state_uuid: str
    ) -> None:
        url = f"{self._project_url(project_id)}/issues/{work_item_id}/"
        await self._patch(url, json={"state": state_uuid})

    async def list_modules(self, project_id: str) -> list[dict]:
        """Return every module in the project (paginated, unwrapped).

        Used by the Plane webhook handler as a fallback when the
        ``issue.updated`` payload omits ``module_ids``. pylon
        enumerates every module and checks each one's work-item list
        for membership.

        Why this direction: Plane CE's external REST
        :class:`IssueSerializer` (``apps/api/plane/api/serializers/issue.py``)
        does not expose ticket → modules linkage. The link lives in
        the ``IssueModule`` join table that the internal app
        serializer pulls in but the external one omits. PR #10's
        work-item-fetch fallback was removed in this commit because
        the field genuinely isn't there; the only way to discover
        membership is module → items.
        """
        url = f"{self._project_url(project_id)}/modules/"
        out: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"per_page": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await self._get(url, params=params)
            results, cursor = _unwrap_paginated(resp.json())
            out.extend(results)
            if not cursor:
                return out

    async def list_links(self, project_id: str, work_item_id: str) -> list[dict]:
        url = f"{self._project_url(project_id)}/issues/{work_item_id}/links/"
        resp = await self._get(url)
        results, _ = _unwrap_paginated(resp.json())
        return results

    async def add_link(
        self, project_id: str, work_item_id: str, link_url: str, title: str = ""
    ) -> bool:
        """Idempotently attach a link. Returns True if newly created, False if duplicate."""
        for link in await self.list_links(project_id, work_item_id):
            if link.get("url") == link_url:
                return False
        url = f"{self._project_url(project_id)}/issues/{work_item_id}/links/"
        await self._post(url, json={"url": link_url, "title": title or link_url})
        return True

    async def add_comment(
        self, project_id: str, work_item_id: str, comment_html: str
    ) -> None:
        url = f"{self._project_url(project_id)}/issues/{work_item_id}/comments/"
        await self._post(url, json={"comment_html": comment_html})

    async def list_states(self, project_id: str) -> list[dict]:
        url = f"{self._project_url(project_id)}/states/"
        resp = await self._get(url)
        results, _ = _unwrap_paginated(resp.json())
        return results

    # ── Modules ─────────────────────────────────────────────────────
    #
    # Modules are Plane's "epic / milestone" grouping for work items.
    # A module carries a ``status`` string (one of: ``backlog``,
    # ``planned``, ``in-progress``, ``paused``, ``completed``,
    # ``cancelled``). Unlike work-item state, the status is a plain
    # string field — no UUID lookup needed.

    async def get_module(self, project_id: str, module_id: str) -> dict | None:
        """Return the module record, or None if it doesn't exist."""
        url = f"{self._project_url(project_id)}/modules/{module_id}/"
        resp = await self._send("GET", url)
        if resp.status_code == 404:
            return None
        _raise_with_body(resp, "GET", url)
        body = resp.json()
        if not isinstance(body, dict):
            return None
        return body

    async def list_module_work_items(
        self, project_id: str, module_id: str
    ) -> list[dict]:
        """Return every work item belonging to ``module_id``.

        Each item includes its current ``state`` UUID, which the
        caller resolves to a group via :class:`ProjectStates`.
        """
        url = f"{self._project_url(project_id)}/modules/{module_id}/module-issues/"
        items: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"per_page": 100}
            if cursor:
                params["cursor"] = cursor
            resp = await self._get(url, params=params)
            results, cursor = _unwrap_paginated(resp.json())
            items.extend(results)
            if not cursor:
                break
        return items

    async def update_module_status(
        self, project_id: str, module_id: str, status: str
    ) -> None:
        """PATCH the module's ``status`` field. Plane accepts the
        plain string — no UUID lookup needed."""
        url = f"{self._project_url(project_id)}/modules/{module_id}/"
        await self._patch(url, json={"status": status})


def _backoff_delay(attempt: int) -> float:
    """Full-jitter exponential backoff: random point in
    ``[0, min(cap, base * 2**(attempt-1))]``. Jitter spreads a herd of
    concurrent retriers (a batch-merge fans out many webhooks at once)
    so they don't re-collide in lockstep."""
    ceiling = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempt - 1)))
    return random.uniform(0, ceiling)


def _retry_after(resp: httpx.Response) -> float | None:
    """Honour a ``Retry-After`` header (delta-seconds form) when Plane
    sends one on a 429. Returns None for the absent/unparseable/HTTP-date
    forms — the caller falls back to jittered backoff."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        secs = float(raw.strip())
    except ValueError:
        return None
    if secs < 0:
        return None
    return min(secs, _BACKOFF_CAP)


def _raise_with_body(resp: httpx.Response, method: str, url: str) -> None:
    """Like resp.raise_for_status() but logs the response body too.

    httpx's default error message hides the response body, which is exactly
    where Plane puts the human-readable reason ("state with id X not found",
    "permission denied", etc.). We want that in the logs.
    """
    if resp.is_success:
        return
    body_snippet = resp.text[:500] if resp.text else "<empty body>"
    logger.error(
        "Plane API error: %s %s -> %d %s | body: %s",
        method,
        url,
        resp.status_code,
        resp.reason_phrase,
        body_snippet,
    )
    resp.raise_for_status()


def _unwrap_paginated(data: Any) -> tuple[list[dict], str | None]:
    """Plane returns either a bare list or a paginated envelope.

    Normalize to ``(items, next_cursor)`` — cursor is ``None`` when
    there's no next page.

    Plane's paginated response shape includes both a ``next_cursor``
    (always truthy, even at the end of the list — clients can
    navigate "off the end" to an empty sentinel page) AND an explicit
    ``next_page_results`` boolean. The boolean is the load-bearing
    end-of-pagination signal; reading just ``next_cursor`` will spin
    forever on any single-page response (regression caught by pylon
    PR #11's runaway-pagination incident when ``list_modules`` was
    introduced — 49 consecutive page requests on a 16-row list before
    Plane rate-limited).

    When ``next_page_results`` is absent (older Plane releases that
    pre-date the field), fall back to treating any truthy
    ``next_cursor`` as "more pages exist".
    """
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        results = data.get("results") or data.get("issues") or []
        # Authoritative end-of-pagination signal (modern Plane).
        if data.get("next_page_results") is False:
            return results, None
        cursor = data.get("next_cursor") or data.get("next")
        return results, cursor if cursor else None
    return [], None
