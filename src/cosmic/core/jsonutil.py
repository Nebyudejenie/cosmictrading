"""JSON conversion: exact decimals as strings, UTC timestamps, canonical hashing form."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from cosmic.core.clock import isoformat_utc
from cosmic.core.money import canonical_str


def to_jsonable(value: Any) -> Any:
    """Convert to JSON-safe primitives without losing precision."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        raise TypeError("float values are not allowed in persisted JSON; use Decimal or str")
    if isinstance(value, Decimal):
        return canonical_str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return isoformat_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = [to_jsonable(v) for v in value]
        return sorted(items, key=json.dumps) if isinstance(value, set | frozenset) else items
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return to_jsonable(model_dump(mode="python"))
    raise TypeError(f"cannot convert {type(value).__name__} to JSON")


def to_jsonable_lenient(value: Any) -> Any:
    """Like ``to_jsonable`` but floats are allowed (analytics, not financial truth)."""
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        return {str(k): to_jsonable_lenient(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable_lenient(v) for v in value]
    return to_jsonable(value)


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, UTF-8."""
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))
