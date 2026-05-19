from src.github_events import PRRef, parse_pull_request


# ── Action classification (unchanged from prior behavior) ─────────────────


def test_opened_non_draft(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "QF-12: add foo"
    parsed = parse_pull_request(pr_opened)
    assert parsed.action == "opened_ready"
    assert parsed.refs == (PRRef("QF", 12),)
    assert parsed.number == 42


def test_opened_draft(pr_opened: dict) -> None:
    pr_opened["pull_request"]["draft"] = True
    pr_opened["pull_request"]["title"] = "QF-12: WIP"
    parsed = parse_pull_request(pr_opened)
    assert parsed.action == "opened_draft"
    assert parsed.refs == (PRRef("QF", 12),)


def test_reopened_ready(pr_opened: dict) -> None:
    pr_opened["action"] = "reopened"
    pr_opened["pull_request"]["draft"] = False
    pr_opened["pull_request"]["title"] = "QF-12: subject"
    parsed = parse_pull_request(pr_opened)
    assert parsed.action == "opened_ready"


def test_reopened_draft(pr_opened: dict) -> None:
    pr_opened["action"] = "reopened"
    pr_opened["pull_request"]["draft"] = True
    pr_opened["pull_request"]["title"] = "QF-12: subject"
    parsed = parse_pull_request(pr_opened)
    assert parsed.action == "opened_draft"


def test_ready_for_review(pr_opened: dict) -> None:
    pr_opened["action"] = "ready_for_review"
    pr_opened["pull_request"]["draft"] = False
    pr_opened["pull_request"]["title"] = "QF-12: subject"
    parsed = parse_pull_request(pr_opened)
    assert parsed.action == "ready_for_review"


def test_converted_to_draft(pr_opened: dict) -> None:
    pr_opened["action"] = "converted_to_draft"
    pr_opened["pull_request"]["draft"] = True
    pr_opened["pull_request"]["title"] = "QF-12: subject"
    parsed = parse_pull_request(pr_opened)
    assert parsed.action == "converted_to_draft"


def test_merged(pr_merged: dict) -> None:
    parsed = parse_pull_request(pr_merged)
    assert parsed.action == "merged"
    assert parsed.refs == (PRRef("QF", 12),)


def test_closed_unmerged(pr_closed_unmerged: dict) -> None:
    parsed = parse_pull_request(pr_closed_unmerged)
    assert parsed.action == "closed_unmerged"
    assert parsed.refs == (PRRef("QF", 13),)


def test_unhandled_action_is_ignored() -> None:
    payload = {
        "action": "synchronize",
        "pull_request": {
            "title": "QF-1: x",
            "body": "",
            "number": 1,
            "html_url": "",
            "draft": False,
        },
    }
    parsed = parse_pull_request(payload)
    assert parsed.action == "ignored"


# ── Leading-prefix extraction: single ref ────────────────────────────────


def test_bare_ref_at_start_with_colon(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "QF-12: add a thing"
    assert parse_pull_request(pr_opened).refs == (PRRef("QF", 12),)


def test_bare_ref_at_start_with_space(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "QF-12 add a thing"
    assert parse_pull_request(pr_opened).refs == (PRRef("QF", 12),)


def test_bracketed_ref_at_start_with_colon(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "[QF-12]: add a thing"
    assert parse_pull_request(pr_opened).refs == (PRRef("QF", 12),)


def test_bracketed_ref_at_start_with_space(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "[QF-12] add a thing"
    assert parse_pull_request(pr_opened).refs == (PRRef("QF", 12),)


def test_ref_only_no_terminator(pr_opened: dict) -> None:
    """End-of-string is a valid terminator — a title that's literally just
    'QF-12' (e.g. drafting an empty PR before writing the subject) matches."""
    pr_opened["pull_request"]["title"] = "QF-12"
    assert parse_pull_request(pr_opened).refs == (PRRef("QF", 12),)


# ── Leading-prefix extraction: multi-ref bundle ──────────────────────────


def test_bundle_two_refs_whitespace_separated(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "QF-12 QF-13: subject"
    assert parse_pull_request(pr_opened).refs == (PRRef("QF", 12), PRRef("QF", 13))


def test_bundle_three_refs_whitespace_separated(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "QF-12 QF-13 QF-14: bundle"
    assert parse_pull_request(pr_opened).refs == (
        PRRef("QF", 12),
        PRRef("QF", 13),
        PRRef("QF", 14),
    )


def test_bundle_bracketed_multi(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "[QF-12 QF-13]: bundle"
    assert parse_pull_request(pr_opened).refs == (PRRef("QF", 12), PRRef("QF", 13))


def test_bundle_comma_separated(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "QF-12, QF-13: bundle"
    assert parse_pull_request(pr_opened).refs == (PRRef("QF", 12), PRRef("QF", 13))


def test_bundle_dedupes(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "QF-12 QF-12 QF-13: dupes"
    assert parse_pull_request(pr_opened).refs == (PRRef("QF", 12), PRRef("QF", 13))


def test_bundle_cross_project(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "QF-12 PT-7: cross-project bundle"
    assert parse_pull_request(pr_opened).refs == (PRRef("QF", 12), PRRef("PT", 7))


# ── Negative cases: nothing matched ──────────────────────────────────────


def test_ref_in_body_only_is_not_matched(pr_opened: dict) -> None:
    """Body refs are NEVER scanned. A title like 'feat: subject' with a body
    that says 'Closes QF-12' must NOT auto-attach."""
    pr_opened["pull_request"]["title"] = "feat: add a thing"
    pr_opened["pull_request"]["body"] = "Closes QF-12. Also see QF-13."
    assert parse_pull_request(pr_opened).refs == ()


def test_ref_mid_title_is_not_matched(pr_opened: dict) -> None:
    """Mid-title refs (e.g. 'Revert QF-84', 'subject (QF-99 later)') are not
    at the leading prefix and must NOT auto-attach."""
    pr_opened["pull_request"]["title"] = "Revert QF-84 changes"
    assert parse_pull_request(pr_opened).refs == ()


def test_ref_in_trailing_parenthetical_is_not_matched(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "subject (QF-99 follow-up)"
    pr_opened["pull_request"]["body"] = ""
    assert parse_pull_request(pr_opened).refs == ()


def test_title_with_no_ref_returns_empty(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "feat: no ticket reference"
    pr_opened["pull_request"]["body"] = ""
    assert parse_pull_request(pr_opened).refs == ()


def test_conventional_commit_prefix_is_not_a_ref(pr_opened: dict) -> None:
    """Conventional-commit prefixes ('feat:', 'fix:', 'docs:') don't match
    the ref shape (no `-<digits>`), so they correctly fall through."""
    pr_opened["pull_request"]["title"] = "fix: handle empty body"
    assert parse_pull_request(pr_opened).refs == ()


def test_lowercase_prefix_does_not_match(pr_opened: dict) -> None:
    """The `[A-Z]` start anchor means lowercase prefixes won't match —
    `pr-25` references in titles won't be picked up."""
    pr_opened["pull_request"]["title"] = "pr-25 fix: thing"
    assert parse_pull_request(pr_opened).refs == ()


def test_trailing_letters_after_digits_dont_match(pr_opened: dict) -> None:
    """`QF-12Y` isn't a valid ticket ref — the trailing `Y` means the digit
    span continues into a non-digit and the regex fails to lock onto a
    clean ref token."""
    pr_opened["pull_request"]["title"] = "QF-12Y subject"
    pr_opened["pull_request"]["body"] = ""
    assert parse_pull_request(pr_opened).refs == ()


def test_unknown_project_prefix_passes_through_to_resolver(pr_opened: dict) -> None:
    """The regex doesn't validate project existence — it accepts any
    uppercase prefix and lets the resolver log-and-skip unknowns. This
    test documents that behaviour so the regex stays prefix-agnostic."""
    pr_opened["pull_request"]["title"] = "XQF-12: unknown project"
    assert parse_pull_request(pr_opened).refs == (PRRef("XQF", 12),)


def test_empty_body_does_not_crash(pr_opened: dict) -> None:
    pr_opened["pull_request"]["title"] = "QF-12: subject"
    pr_opened["pull_request"]["body"] = None
    parsed = parse_pull_request(pr_opened)
    assert parsed.action == "opened_ready"
    assert parsed.refs == (PRRef("QF", 12),)
