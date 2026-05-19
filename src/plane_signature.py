"""HMAC verification for Plane webhook deliveries.

Plane CE signs each webhook with HMAC-SHA256 over the raw request body
using the per-webhook secret configured in Plane's UI. The hex digest
is sent in the ``x-plane-signature`` header — no scheme prefix.

This is structurally identical to the GitHub variant (see
``github_signature.verify``); the only difference is the header
format. We keep two functions to keep call-site naming explicit
about which event source we're verifying.

If the operator hasn't configured a Plane webhook secret yet
(``PLANE_WEBHOOK_SECRET`` is empty), :func:`verify` returns ``True``
for any input — the caller is expected to log a warning when running
unsigned. This mirrors Plane CE's behaviour, where webhook secrets
are optional.
"""

from __future__ import annotations

import hashlib
import hmac


def verify(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Return True when the request body matches the signature header.

    ``secret`` may be empty — that disables verification entirely
    (verify becomes a no-op returning True). When ``secret`` is set,
    ``signature_header`` must be the hex digest of HMAC-SHA256 over
    ``body`` keyed by ``secret``. Comparisons use ``hmac.compare_digest``
    to avoid timing leaks.
    """
    if not secret:
        # Unsigned mode — caller is responsible for surfacing this
        # operationally. Return True so the request flows through.
        return True
    if not signature_header:
        return False
    presented = signature_header.strip().lower()
    # Accept both bare hex and `sha256=...` prefixed forms — Plane CE
    # has shipped both at different versions, and being permissive is
    # cheap.
    if presented.startswith("sha256="):
        presented = presented[len("sha256=") :]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented)
