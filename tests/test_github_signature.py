import hashlib
import hmac

from src.github_signature import verify


SECRET = "test-secret-do-not-use"
BODY = b'{"action":"opened","pull_request":{"number":1}}'
GOOD_SIG = "sha256=" + hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()


def test_valid_signature() -> None:
    assert verify(SECRET, BODY, GOOD_SIG) is True


def test_tampered_signature() -> None:
    bad = GOOD_SIG[:-1] + ("0" if GOOD_SIG[-1] != "0" else "1")
    assert verify(SECRET, BODY, bad) is False


def test_tampered_body() -> None:
    assert verify(SECRET, BODY + b"!", GOOD_SIG) is False


def test_wrong_secret() -> None:
    assert verify("other-secret", BODY, GOOD_SIG) is False


def test_missing_header() -> None:
    assert verify(SECRET, BODY, None) is False
    assert verify(SECRET, BODY, "") is False


def test_wrong_algorithm_prefix() -> None:
    sig = "sha1=" + hmac.new(SECRET.encode(), BODY, hashlib.sha1).hexdigest()
    assert verify(SECRET, BODY, sig) is False


def test_missing_prefix() -> None:
    raw_hex = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert verify(SECRET, BODY, raw_hex) is False
