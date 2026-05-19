"""Tests for ``src.plane_signature.verify``."""

import hashlib
import hmac

import pytest

from src.plane_signature import verify


SECRET = "test-secret"
BODY = b'{"event":"issue","action":"updated"}'


def _hex_sig(secret: str = SECRET, body: bytes = BODY) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestVerify:
    def test_valid_hex_signature_passes(self) -> None:
        assert verify(SECRET, BODY, _hex_sig()) is True

    def test_invalid_hex_signature_fails(self) -> None:
        assert verify(SECRET, BODY, "a" * 64) is False

    def test_truncated_signature_fails(self) -> None:
        assert verify(SECRET, BODY, _hex_sig()[:32]) is False

    def test_uppercase_hex_is_accepted(self) -> None:
        # Plane / proxies sometimes uppercase the digest — verify
        # normalises before compare.
        assert verify(SECRET, BODY, _hex_sig().upper()) is True

    @pytest.mark.parametrize("prefix", ["sha256=", "SHA256="])
    def test_prefixed_form_is_accepted(self, prefix: str) -> None:
        # Older Plane releases emitted `sha256=<hex>` style. Newer
        # ones emit bare hex. Be permissive.
        assert verify(SECRET, BODY, prefix + _hex_sig()) is True

    def test_missing_header_fails_when_secret_set(self) -> None:
        assert verify(SECRET, BODY, None) is False
        assert verify(SECRET, BODY, "") is False

    def test_empty_secret_is_unsigned_mode(self) -> None:
        # When the operator hasn't configured a secret, we accept all
        # deliveries (the route logs a warning at startup).
        assert verify("", BODY, None) is True
        assert verify("", BODY, "anything") is True

    def test_body_tamper_fails(self) -> None:
        sig = _hex_sig(body=BODY)
        tampered = BODY + b" "
        assert verify(SECRET, tampered, sig) is False

    def test_wrong_secret_fails(self) -> None:
        sig = _hex_sig(secret="some-other-secret")
        assert verify(SECRET, BODY, sig) is False
