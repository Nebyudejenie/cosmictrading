"""Import every table module so ``metadata`` is complete (drift test, tooling)."""

from __future__ import annotations

from cosmic.db import schema, schema_market
from cosmic.db.schema import metadata

MODULES = (schema, schema_market)

__all__ = ["MODULES", "metadata"]
