"""Tests for ``src.plane_client._unwrap_paginated``.

The function normalises Plane's three paginated response shapes (bare
list, dict-with-cursor, modern-envelope-with-next_page_results) to a
single ``(items, next_cursor)`` tuple. The end-of-pagination signal
matters here — pylon PR #11's runaway-pagination incident was caused
by treating any truthy ``next_cursor`` as "more pages", which Plane
always returns regardless of whether more results exist.
"""

from __future__ import annotations

from src.plane_client import _unwrap_paginated


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
