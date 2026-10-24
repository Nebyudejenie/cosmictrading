"""Daily rules for the auto-trader (ADR-030 addendum 5): a mandate may run on daily bars.

- ``trading_mandates.timeframe``: '1h' (completed hourly bars, as before) or '1d' (completed
  daily bars, decided once a day after 00:00 UTC). The guard trigger already freezes every
  column but the status fields, so the timeframe is immutable like the other terms.

The downgrade refuses while a daily mandate exists (nothing is deleted).

Revision ID: 0015_mandate_timeframe
Revises: 0014_auto_competitions
Create Date: 2026-10-24
"""

from cosmic.db.sqlsplit import run_script

revision = "0015_mandate_timeframe"
down_revision = "0014_auto_competitions"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    ALTER TABLE trading_mandates DROP CONSTRAINT trading_mandates_timeframe_check;
    ALTER TABLE trading_mandates ADD CONSTRAINT trading_mandates_timeframe_check
      CHECK (timeframe IN ('1h','1d'));
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM trading_mandates WHERE timeframe <> '1h') THEN
        RAISE EXCEPTION 'daily mandates exist; the schema before 0015 cannot hold them';
      END IF;
    END $$;
    ALTER TABLE trading_mandates DROP CONSTRAINT trading_mandates_timeframe_check;
    ALTER TABLE trading_mandates ADD CONSTRAINT trading_mandates_timeframe_check
      CHECK (timeframe IN ('1h'));
    """)
