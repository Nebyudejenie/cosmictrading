"""The bridge's fast lane (S87, the owner's rule of 2026-11-12: an order leaves for HFM within
one second of its prediction).

- ``bridge_fast``: HFM's newest prices as a bridge sends them a few times a second on its fast
  lane, with the bridge's own measurements (clock, round trip, how long its report takes). One
  row per bridge, replaced on every request, kept apart from ``bridge_state``: that one is the
  whole account as last reported, and its age says how old the positions and deals are.

Nothing here is evidence: the downgrade drops the table.

Revision ID: 0032_bridge_fast
Revises: 0031_gold_predictions
Create Date: 2026-11-12
"""

from cosmic.db.sqlsplit import run_script

revision = "0032_bridge_fast"
down_revision = "0031_gold_predictions"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    CREATE TABLE bridge_fast (
      bridge text PRIMARY KEY,
      received_at timestamptz NOT NULL,
      ticks jsonb NOT NULL,
      server_utc_offset_ms integer,
      info jsonb NOT NULL DEFAULT '{}'::jsonb
    );
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DROP TABLE bridge_fast;
    """)
