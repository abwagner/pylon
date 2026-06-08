"""Tests for ``src.plane_client._unwrap_paginated``.

The function normalises Plane's three paginated response shapes (bare
list, dict-with-cursor, modern-envelope-with-next_page_results) to a
single ``(items, next_cursor)`` tuple. The end-of-pagination signal
matters here — pylon PR #11's runaway-pagination incident was caused
by treating any truthy ``next_cursor`` as "more pages", which Plane
always returns regardless of whether more results exist.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from src.plane_client import (
    PlaneClient,
    _backoff_delay,
    _retry_after,
    _unwrap_paginated,
)


# ── Bare-list shape (some older Plane endpoints) ───────────────────


def test_bare_list_returns_items_with_no_cursor() -> None:
    items, cursor = _unwrap_paginated([{"id": "a"}, {"id": "b"}])
    assert items == [{"id": "a"}, {"id": "b"}]
    assert cursor is None


def test_empty_bare_list() -> None:
    assert _unwrap_paginated([]) == ([], None)


# ── Modern envelope with explicit next_page_results ────────────────


def test_single_page_modern_envelope_stops_pagination() -> None:
    """The regression: Plane returns a truthy next_cursor even on a
    single-page response. pylon MUST use next_page_results=False as
    the end-of-list signal — otherwise list endpoints loop forever."""
    page = {
        "count": 16,
        "results": [{"id": f"m{i}"} for i in range(16)],
        "next_cursor": "100:1:0",  # truthy, but...
        "next_page_results": False,  # ...this says we're done.
        "prev_cursor": "100:-1:1",
        "prev_page_results": False,
        "total_count": 16,
        "total_pages": 1,
    }
    items, cursor = _unwrap_paginated(page)
    assert len(items) == 16
    assert cursor is None  # not "100:1:0"


def test_multi_page_modern_envelope_returns_cursor() -> None:
    page = {
        "count": 100,
        "results": [{"id": f"i{i}"} for i in range(100)],
        "next_cursor": "100:1:0",
        "next_page_results": True,
        "total_count": 250,
    }
    items, cursor = _unwrap_paginated(page)
    assert len(items) == 100
    assert cursor == "100:1:0"


# ── Older envelope (no next_page_results field) ────────────────────


def test_legacy_envelope_uses_next_cursor() -> None:
    """Older Plane releases don't ship next_page_results. Fall back
    to "any truthy cursor means more pages" — same as before."""
    page = {
        "results": [{"id": "a"}],
        "next_cursor": "100:1:0",
    }
    items, cursor = _unwrap_paginated(page)
    assert items == [{"id": "a"}]
    assert cursor == "100:1:0"


def test_legacy_envelope_empty_cursor_terminates() -> None:
    page = {"results": [{"id": "a"}], "next_cursor": None}
    items, cursor = _unwrap_paginated(page)
    assert items == [{"id": "a"}]
    assert cursor is None


def test_legacy_envelope_falsy_next_field() -> None:
    page = {"results": [], "next": ""}
    items, cursor = _unwrap_paginated(page)
    assert items == []
    assert cursor is None


# ── Edge cases ─────────────────────────────────────────────────────


def test_missing_results_returns_empty() -> None:
    page = {"next_page_results": False}
    items, cursor = _unwrap_paginated(page)
    assert items == []
    assert cursor is None


def test_issues_alias_is_recognised() -> None:
    # Some Plane endpoints return the items under "issues" instead of "results".
    page = {"issues": [{"id": "wi-1"}], "next_page_results": False}
    items, cursor = _unwrap_paginated(page)
    assert items == [{"id": "wi-1"}]
    assert cursor is None


def test_non_dict_non_list_returns_empty() -> None:
    assert _unwrap_paginated(None) == ([], None)
    assert _unwrap_paginated("not json") == ([], None)
    assert _unwrap_paginated(42) == ([], None)


def test_next_page_results_true_with_no_cursor_returns_none() -> None:
    # Pathological — next_page_results says "more" but no cursor.
    # Treat as terminated to avoid infinite loops on malformed pages.
    page = {
        "results": [{"id": "a"}],
        "next_page_results": True,
        "next_cursor": None,
    }
    items, cursor = _unwrap_paginated(page)
    assert items == [{"id": "a"}]
    assert cursor is None


# ── Retry-with-backoff on transient Plane-API failures ─────────────
#
# The keystone of the batch-merge data-loss fix: when Plane CE
# saturates under a rapid merge burst it emits 429 / 5xx / timeouts.
# Previously each one bubbled straight up, dropped the ticket
# transition, and (because the webhook still answered 200) was never
# retried. PlaneClient now retries these transient classes itself
# before the GitHub-delivery retry ever has to.


@pytest.fixture
def client() -> PlaneClient:
    return PlaneClient(
        base_url="https://plane.test",
        workspace_slug="ws",
        api_key="k",
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real backoff wait so retry tests run instantly. We
    patch the sleep itself rather than zeroing the backoff constants —
    those constants are also read by _retry_after, which the helper
    unit tests below assert against."""
    import src.plane_client as pc

    async def _instant(_delay: float) -> None:
        return None

    monkeypatch.setattr(pc.asyncio, "sleep", _instant)


_STATES_URL = "https://plane.test/api/v1/workspaces/ws/projects/p1/states/"
_ISSUE_URL = "https://plane.test/api/v1/workspaces/ws/projects/p1/issues/wi-1/"


@respx.mock
async def test_retries_503_then_succeeds(client: PlaneClient) -> None:
    route = respx.get(_STATES_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"results": [{"id": "s1"}], "next_page_results": False}),
        ]
    )
    states = await client.list_states("p1")
    assert states == [{"id": "s1"}]
    assert route.call_count == 3
    await client.aclose()


@respx.mock
async def test_retries_429_then_succeeds(client: PlaneClient) -> None:
    route = respx.patch(_ISSUE_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={}),
        ]
    )
    # Should not raise — the second attempt lands the state PATCH.
    await client.update_state("p1", "wi-1", "state-uuid")
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_retries_on_network_timeout_then_succeeds(client: PlaneClient) -> None:
    route = respx.get(_STATES_URL).mock(
        side_effect=[
            httpx.ConnectTimeout("boom"),
            httpx.Response(200, json={"results": [], "next_page_results": False}),
        ]
    )
    states = await client.list_states("p1")
    assert states == []
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_exhausts_retries_and_raises_on_persistent_5xx(client: PlaneClient) -> None:
    route = respx.patch(_ISSUE_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await client.update_state("p1", "wi-1", "state-uuid")
    # _MAX_ATTEMPTS total tries before giving up.
    assert route.call_count == 4
    await client.aclose()


@respx.mock
async def test_exhausts_retries_and_raises_on_persistent_timeout(client: PlaneClient) -> None:
    route = respx.get(_STATES_URL).mock(side_effect=httpx.ReadTimeout("nope"))
    with pytest.raises(httpx.ReadTimeout):
        await client.list_states("p1")
    assert route.call_count == 4
    await client.aclose()


@respx.mock
async def test_non_retryable_4xx_raises_immediately(client: PlaneClient) -> None:
    route = respx.get(_STATES_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await client.list_states("p1")
    # 404 is a real answer, not a transient blip — no retry.
    assert route.call_count == 1
    await client.aclose()


# ── Backoff / Retry-After helpers ──────────────────────────────────


def test_backoff_delay_within_jittered_ceiling() -> None:
    import src.plane_client as pc

    for attempt in range(1, 6):
        ceiling = min(pc._BACKOFF_CAP, pc._BACKOFF_BASE * (2 ** (attempt - 1)))
        for _ in range(50):
            d = _backoff_delay(attempt)
            assert 0.0 <= d <= ceiling


def test_retry_after_parses_delta_seconds() -> None:
    resp = httpx.Response(429, headers={"Retry-After": "2"})
    assert _retry_after(resp) == 2.0


def test_retry_after_caps_at_backoff_cap() -> None:
    import src.plane_client as pc

    resp = httpx.Response(429, headers={"Retry-After": "9999"})
    assert _retry_after(resp) == pc._BACKOFF_CAP


def test_retry_after_absent_returns_none() -> None:
    assert _retry_after(httpx.Response(429)) is None


def test_retry_after_http_date_form_returns_none() -> None:
    # We only handle the delta-seconds form; HTTP-date falls back to
    # jittered backoff rather than mis-parsing.
    resp = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert _retry_after(resp) is None
