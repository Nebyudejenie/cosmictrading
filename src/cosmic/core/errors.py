"""Error types shared across the application.

Messages on these errors are safe to show to an operator: they never contain
secrets, stack traces or internal host names.
"""

from __future__ import annotations


class CosmicError(Exception):
    """Base class for expected, user-presentable errors."""

    code = "error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ConfigError(CosmicError):
    code = "config_invalid"


class NotFoundError(CosmicError):
    code = "not_found"


class PermissionDeniedError(CosmicError):
    code = "forbidden"


class StepUpRequiredError(PermissionDeniedError):
    code = "step_up_required"


class AuthenticationError(CosmicError):
    code = "unauthenticated"


class ConflictError(CosmicError):
    """State conflict: expired, consumed, superseded, precondition unmet."""

    code = "conflict"


class ValidationFailedError(CosmicError):
    code = "validation_failed"


class RateLimitedError(CosmicError):
    code = "rate_limited"
