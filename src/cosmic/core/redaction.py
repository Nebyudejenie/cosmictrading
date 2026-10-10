"""Secret redaction for logs, audit payloads and outbound AI prompts (PRIN-12, OBS-01)."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"

SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|cookie|totp|otp|csrf|dsn|"
    r"private[_-]?key|credential|session[_-]?id|mfa)",
    re.IGNORECASE,
)

# Patterns for well-known credential formats (value-based detection).
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),  # Groq
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{20,}\b"),  # OpenRouter
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),  # OpenAI-style
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),  # JWT / tunnel
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"postgres(ql)?(\+\w+)?://[^:\s/]+:[^@\s]+@"),  # DSN with password
)


class SecretRegistry:
    """Process-wide set of configured secret values that must never be emitted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: tuple[str, ...] = ()

    def register(self, values: Iterable[str]) -> None:
        with self._lock:
            merged = set(self._values)
            merged.update(v for v in values if v and len(v) >= 6)
            # Longest first so overlapping secrets are fully masked.
            self._values = tuple(sorted(merged, key=len, reverse=True))

    def clear(self) -> None:
        with self._lock:
            self._values = ()

    @property
    def values(self) -> tuple[str, ...]:
        return self._values


SECRETS = SecretRegistry()


def scrub_text(text: str) -> str:
    for value in SECRETS.values:
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def contains_secret(text: str) -> bool:
    if any(value in text for value in SECRETS.values):
        return True
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact sensitive keys and secret-looking values."""
    if _depth > 12:
        return REDACTED
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if SENSITIVE_KEY.search(key_str):
                out[key_str] = REDACTED
            else:
                out[key_str] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value
