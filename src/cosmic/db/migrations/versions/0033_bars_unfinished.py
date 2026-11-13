"""Unfinished candles are found at once (S89).

Every price a bridge sends closes the candles before the one it belongs to: an UPDATE of the
earlier candles that are not complete yet. There is at most one such candle a timeframe, but
finding it read every candle stored: 0.7 s for the 1-minute candles and 1.2 s in all once HFM's
four years of history were stored (1.8 million candles; measured in production, 2026-11-13).
It slowed every report of the bridge, and with the fast lane (S87) every new price.

- ``bars_unfinished``: the candles that are not complete, by instrument, source, timeframe and
  time. A handful of rows; the UPDATE finds its candle through it at any history size.

Revision ID: 0033_bars_unfinished
Revises: 0032_bridge_fast
Create Date: 2026-11-13
"""

from cosmic.db.sqlsplit import run_script

revision = "0033_bars_unfinished"
down_revision = "0032_bridge_fast"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    CREATE INDEX bars_unfinished ON bars (instrument_id, source_id, timeframe, open_time)
      WHERE NOT is_complete;
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DROP INDEX bars_unfinished;
    """)
