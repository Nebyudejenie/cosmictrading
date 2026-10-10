"""TOTP multi-factor authentication with encrypted secrets and replay protection."""

from __future__ import annotations

import os
from datetime import datetime

import pyotp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ISSUER = "COSMICTRADING QUANT"
_AAD = b"cosmic-mfa-v1"
STEP_SECONDS = 30


def generate_secret() -> str:
    return pyotp.random_base32(length=32)


def encrypt_secret(key: bytes, secret: str) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, secret.encode("ascii"), _AAD)


def decrypt_secret(key: bytes, blob: bytes) -> str:
    return AESGCM(key).decrypt(blob[:12], blob[12:], _AAD).decode("ascii")


def provisioning_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)


def current_step(now: datetime) -> int:
    return int(now.timestamp()) // STEP_SECONDS


def verify_code(secret: str, code: str, now: datetime, last_step: int | None) -> int | None:
    """Return the matched time step, or None.

    Accepts the current step and one step either side (clock drift). A step at or
    before ``last_step`` is rejected so a code cannot be replayed.
    """
    code = code.strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    step = current_step(now)
    for candidate in (step - 1, step, step + 1):
        if last_step is not None and candidate <= last_step:
            continue
        if pyotp.utils.strings_equal(totp.at(candidate * STEP_SECONDS), code):
            return candidate
    return None


def code_at(secret: str, when: datetime) -> str:
    """Current code for a secret (tests and enrolment previews only)."""
    return str(pyotp.TOTP(secret).at(when))
