"""The total loss stop (ADR-030 addendum 3): the auto-trader switches itself off for good
once its losses under a mandate leave no room for another trade.

- ``trading_mandates.total_loss_limit``: the most the auto-trader may lose in total under
  the mandate. NULL for mandates granted before; the code then uses ten trades' worth.
- status ``STOPPED``: the mandate was ended by the auto-trader itself at that limit (besides
  REVOKED and EXPIRED). An ended mandate stays ended, as before.

The guard trigger already freezes every column but the status fields, so the limit is
immutable too. The downgrade refuses while a mandate uses the limit or is STOPPED (nothing
is deleted).

Revision ID: 0013_mandate_total_stop
Revises: 0012_mandate_money
Create Date: 2026-10-22
"""

from cosmic.db.sqlsplit import run_script

revision = "0013_mandate_total_stop"
down_revision = "0012_mandate_money"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    ALTER TABLE trading_mandates
      ADD COLUMN total_loss_limit numeric(38,18)
        CHECK (total_loss_limit IS NULL OR total_loss_limit > 0);
    ALTER TABLE trading_mandates DROP CONSTRAINT trading_mandates_status_check;
    ALTER TABLE trading_mandates ADD CONSTRAINT trading_mandates_status_check
      CHECK (status IN ('ACTIVE','REVOKED','EXPIRED','STOPPED'));
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM trading_mandates WHERE total_loss_limit IS NOT NULL OR status = 'STOPPED'
      ) THEN
        RAISE EXCEPTION 'mandates use the total loss stop; the schema before 0013 cannot hold them';
      END IF;
    END $$;
    ALTER TABLE trading_mandates DROP CONSTRAINT trading_mandates_status_check;
    ALTER TABLE trading_mandates ADD CONSTRAINT trading_mandates_status_check
      CHECK (status IN ('ACTIVE','REVOKED','EXPIRED'));
    ALTER TABLE trading_mandates DROP COLUMN total_loss_limit;
    """)
