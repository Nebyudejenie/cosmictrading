"""Mandates from money (ADR-030 addendum): the owner puts in an amount and a care level.

- ``trading_mandates.capital``: the money the auto-trader may use. Its open positions, held
  fully paid (ADR-028), may not be worth more than this. NULL for mandates granted before.
- ``trading_mandates.risk_pct``: the care level, the share of that money a trade may lose at
  its stop (0.1–5 %). The maximum loss per trade is derived from it when granted.
- ``trading_mandates.mode``: AUTO approves within the mandate; ASK proposes and asks the owner
  ("time to trade") through the normal approval.

The guard trigger already freezes every column but the status fields, so the new terms are
immutable too. The downgrade refuses while a mandate uses them (nothing is deleted).

Revision ID: 0012_mandate_money
Revises: 0011_demo_mandates
Create Date: 2026-10-22
"""

from cosmic.db.sqlsplit import run_script

revision = "0012_mandate_money"
down_revision = "0011_demo_mandates"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    ALTER TABLE trading_mandates
      ADD COLUMN capital numeric(38,18) CHECK (capital IS NULL OR capital > 0),
      ADD COLUMN risk_pct numeric(38,18) CHECK (risk_pct IS NULL OR (risk_pct >= 0.1 AND risk_pct <= 5)),
      ADD COLUMN mode text NOT NULL DEFAULT 'AUTO' CHECK (mode IN ('AUTO','ASK'));
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM trading_mandates
        WHERE capital IS NOT NULL OR risk_pct IS NOT NULL OR mode <> 'AUTO'
      ) THEN
        RAISE EXCEPTION 'mandates use money or ASK mode; the schema before 0012 cannot hold them';
      END IF;
    END $$;
    ALTER TABLE trading_mandates DROP COLUMN mode, DROP COLUMN risk_pct, DROP COLUMN capital;
    """)
