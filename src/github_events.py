import re
from dataclasses import dataclass
from typing import Literal


# A semantic event derived from a GitHub pull_request webhook. The state
# machine in handler.py keys off of these.
Action = Literal[
    "opened_draft",        # PR opened (or reopened) as draft
    "opened_ready",        # PR opened (or reopened) ready for review
    "ready_for_review",    # transitioned draft → ready
    "converted_to_draft",  # transitioned ready → draft
    "merged",              # closed with merge=true
    "closed_unmerged",     # closed with merge=false
    "ignored",             # any other event we don't act on
]


@dataclass(frozen=True)
class PRRef:
    project: str   # e.g. "QF"
    sequence: int  # e.g. 12

    def __str__(self) -> str:
        return f"{self.project}-{self.sequence}"


@dataclass(frozen=True)
class ParsedPullRequest:
    action: Action
    number: int
    url: str
    title: str
    refs: tuple[PRRef, ...]


# Match Plane work-item references ONLY at the start of the PR title.
# Three supported shapes (with or without brackets around the whole group):
#
#   QF-12: subject                  → [QF-12]
#   [QF-12] subject                 → [QF-12]
#   QF-12 QF-13: subject            → [QF-12, QF-13]
#   [QF-12 QF-13 QF-14] subject     → [QF-12, QF-13, QF-14]
#   QF-12, QF-13: subject           → [QF-12, QF-13]
#
# Anything past the leading ref-prefix is ignored — body text, mid-title
# refs ("Revert QF-84"), trailing parentheticals ("subject (QF-99 later)")
# all stay out of the resolver. The leading-prefix discipline is the
# whole point of this regex: a PR body that mentions adjacent tickets
# ("future work in QF-94 / QF-91") must NOT auto-attach to those tickets.
#
# The pattern accepts any uppercase prefix; the resolver logs-and-skips
# refs whose project prefix isn't a known Plane project (e.g. `PR-25`
# for a GitHub PR ref). False positives are still bounded — limited to
# refs that LOOK like they could be tickets and happen to appear at the
# leading-prefix position.
_LEADING_PREFIX_PATTERN = re.compile(
    r"^\[?\s*"                                   # optional opening bracket + whitespace
    r"(?P<prefix>"
    r"[A-Z][A-Z0-9_]*-\d+"                       # first ref
    r"(?:[,\s]+[A-Z][A-Z0-9_]*-\d+)*"            # additional refs separated by ws/comma
    r")"
    r"\s*\]?"                                    # optional closing bracket
    r"(?:\s|:|$)"                                # terminator: ws, colon, or end-of-title
)

_REF_TOKEN_PATTERN = re.compile(r"([A-Z][A-Z0-9_]*)-(\d+)")


def parse_pull_request(payload: dict) -> ParsedPullRequest:
    pr = payload.get("pull_request") or {}
    action = _classify(payload.get("action"), pr)
    title = pr.get("title") or ""
    return ParsedPullRequest(
        action=action,
        number=pr.get("number") or 0,
        url=pr.get("html_url") or "",
        title=title,
        refs=tuple(_extract_leading_refs(title)),
    )


def _classify(action: str | None, pr: dict) -> Action:
    if action in ("opened", "reopened"):
        return "opened_draft" if pr.get("draft") else "opened_ready"
    if action == "ready_for_review":
        return "ready_for_review"
    if action == "converted_to_draft":
        return "converted_to_draft"
    if action == "closed":
        return "merged" if pr.get("merged") else "closed_unmerged"
    return "ignored"


def _extract_leading_refs(title: str) -> list[PRRef]:
    """Extract refs from the leading-prefix of the PR title only.

    Returns empty list if the title doesn't start with a ref-prefix
    block (i.e. PRs whose title is `Revert ...` or `feat: ...` with no
    leading `QF-N` go through unattached). Body text is never scanned.
    """
    m = _LEADING_PREFIX_PATTERN.match(title)
    if not m:
        return []
    prefix = m.group("prefix")
    seen: set[tuple[str, int]] = set()
    out: list[PRRef] = []
    for ref_m in _REF_TOKEN_PATTERN.finditer(prefix):
        key = (ref_m.group(1), int(ref_m.group(2)))
        if key in seen:
            continue
        seen.add(key)
        out.append(PRRef(project=key[0], sequence=key[1]))
    return out
