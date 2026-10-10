"""Password hashing (argon2id) and policy."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from cosmic.core.errors import ValidationFailedError

_hasher = PasswordHasher()  # argon2id with library defaults (RFC 9106 low-memory profile)

MIN_LENGTH = 12
MAX_LENGTH = 256
_COMMON = frozenset(
    {
        "password1234",
        "123456789012",
        "qwertyuiopas",
        "letmein12345",
        "passwordpassword",
        "adminadmin12",
        "welcome12345",
        "iloveyou1234",
        "changeme1234",
        "trustno11234",
    }
)

# Pre-computed hash used to equalise timing when the username does not exist.
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


def validate_password(password: str, username: str) -> None:
    if len(password) < MIN_LENGTH:
        raise ValidationFailedError(f"password must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        raise ValidationFailedError(f"password must be at most {MAX_LENGTH} characters")
    lowered = password.lower()
    if lowered in _COMMON or username.lower() in lowered or len(set(password)) < 6:
        raise ValidationFailedError("password is too easy to guess")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
