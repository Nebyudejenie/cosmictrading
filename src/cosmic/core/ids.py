"""Identifiers: UUIDv7 primary keys and human-readable trade codes (ADR-008)."""

from __future__ import annotations

import os
import secrets
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def uuid7() -> uuid.UUID:
    """Return an RFC 9562 version-7 UUID.

    48-bit Unix millisecond timestamp, then a 12-bit counter (rand_a) that keeps
    IDs generated within the same millisecond in this process strictly
    increasing, then 62 random bits (rand_b).
    """
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _last_ms = ms
            _counter = secrets.randbits(11)  # leave headroom before overflow
        else:
            ms = _last_ms
            _counter += 1
            if _counter > 0xFFF:
                # Counter exhausted within one millisecond: advance the timestamp.
                _last_ms += 1
                ms = _last_ms
                _counter = secrets.randbits(11)
        counter = _counter
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (counter & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Extract the millisecond timestamp from a UUIDv7."""
    if value.version != 7:
        raise ValueError("not a UUIDv7")
    return value.int >> 80


def random_code(length: int = 4) -> str:
    """Crockford base32 random code (no ambiguous characters)."""
    return "".join(secrets.choice(_CROCKFORD) for _ in range(length))


def trade_code(day_yyyymmdd: str, length: int = 4) -> str:
    """Human-facing trade code, e.g. ``T-20261010-7F3K``. Not a foreign key."""
    return f"T-{day_yyyymmdd}-{random_code(length)}"
