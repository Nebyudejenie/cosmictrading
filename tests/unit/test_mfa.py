from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag

from cosmic.identity import mfa

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)


def test_encrypt_roundtrip_and_tamper() -> None:
    key = b"k" * 32
    secret = mfa.generate_secret()
    blob = mfa.encrypt_secret(key, secret)
    assert secret.encode() not in blob
    assert mfa.decrypt_secret(key, blob) == secret
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(InvalidTag):
        mfa.decrypt_secret(key, tampered)
    with pytest.raises(InvalidTag):
        mfa.decrypt_secret(b"j" * 32, blob)


def test_verify_accepts_current_and_rejects_replay() -> None:
    secret = mfa.generate_secret()
    code = mfa.code_at(secret, NOW)
    step = mfa.verify_code(secret, code, NOW, last_step=None)
    assert step == mfa.current_step(NOW)
    assert mfa.verify_code(secret, code, NOW, last_step=step) is None  # replay


def test_verify_drift_window_and_garbage() -> None:
    secret = mfa.generate_secret()
    old = mfa.code_at(secret, NOW - timedelta(seconds=30))
    assert mfa.verify_code(secret, old, NOW, None) is not None
    very_old = mfa.code_at(secret, NOW - timedelta(seconds=120))
    assert mfa.verify_code(secret, very_old, NOW, None) is None
    for bad in ("", "12345", "abcdef", "1234567"):
        assert mfa.verify_code(secret, bad, NOW, None) is None
