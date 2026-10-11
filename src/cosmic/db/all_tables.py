"""Import every table module so ``metadata`` is complete (drift test, tooling)."""

from __future__ import annotations

from cosmic.db import schema, schema_ai, schema_execution, schema_market, schema_trading
from cosmic.db.schema import metadata

MODULES = (schema, schema_market, schema_trading, schema_execution, schema_ai)

__all__ = ["MODULES", "metadata"]
