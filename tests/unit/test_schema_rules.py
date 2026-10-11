"""Schema rules (ARC-08, ARC-09): no binary floating point anywhere, UTC-aware timestamps,
money at NUMERIC(38,18). The drift test ties this metadata to the migrated database."""

from __future__ import annotations

from sqlalchemy import DateTime, Float, Numeric

from cosmic.db.all_tables import metadata

MONEY_HINTS = (
    "price",
    "qty",
    "quantity",
    "amount",
    "notional",
    "fee",
    "pnl",
    "equity",
    "balance",
    "capital",
    "cost",
    "exposure",
    "hold_amount",
    "high_water_mark",
)


def test_no_floating_point_columns() -> None:
    floats = [
        f"{t.name}.{c.name}"
        for t in metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, Float) or (isinstance(c.type, Numeric) and c.type.asdecimal is False)
    ]
    assert not floats, floats


def test_every_timestamp_is_timezone_aware() -> None:
    naive = [
        f"{t.name}.{c.name}"
        for t in metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, DateTime) and not c.type.timezone
    ]
    assert not naive, naive


def test_money_columns_use_numeric_38_18() -> None:
    wrong = []
    for t in metadata.tables.values():
        for c in t.columns:
            is_money = isinstance(c.type, Numeric) and any(h in c.name for h in MONEY_HINTS)
            if is_money and (c.type.precision, c.type.scale) != (38, 18):
                wrong.append(f"{t.name}.{c.name} {c.type}")
    assert not wrong, wrong
    numeric_money = sum(
        1
        for t in metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, Numeric) and any(h in c.name for h in MONEY_HINTS)
    )
    assert numeric_money > 30  # the rule is actually exercised
