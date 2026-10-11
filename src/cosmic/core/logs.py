"""Structured JSON logging with correlation IDs and redaction (OBS-01)."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

from cosmic.core.redaction import redact

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def get_correlation_id() -> str:
    value = correlation_id_var.get()
    if value is None:
        value = new_correlation_id()
        correlation_id_var.set(value)
    return value


def _add_correlation_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    cid = correlation_id_var.get()
    if cid is not None:
        event_dict.setdefault("correlation_id", cid)
    return event_dict


def _redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    redacted = redact(dict(event_dict))
    assert isinstance(redacted, dict)
    return redacted


class _LazyStdout:
    """Resolves sys.stdout at write time (robust to stream replacement, e.g. in tests)."""

    def write(self, text: str) -> int:
        return sys.stdout.write(text)

    def flush(self) -> None:
        sys.stdout.flush()


class _LazyStreamHandler(logging.StreamHandler):  # type: ignore[type-arg]
    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, _value: Any) -> None:
        return None


def configure_logging(
    level: str = "INFO", json_output: bool = True, process: str = "cosmic"
) -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_correlation_id,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.format_exc_info,
        _redact_processor,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=_LazyStdout()),  # type: ignore[arg-type]
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.bind_contextvars(process=process)
    # Route stdlib logging (uvicorn, asyncpg, alembic) through a redacting handler.
    handler = _LazyStreamHandler()
    handler.addFilter(_StdlibRedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class _StdlibRedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        from cosmic.core.redaction import scrub_text

        record.msg = scrub_text(record.getMessage())
        record.args = ()
        return True


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
