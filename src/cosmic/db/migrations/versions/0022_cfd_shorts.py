"""Genuine shorts on a CFD (S66, ADR-037 addendum): a position may be below zero, a short, only on
an instrument whose asset class is CFD and only on a simulated account (PAPER or SANDBOX: the
HFM demo). Spot holdings and real money stay long-only.

- ``positions``: the ``quantity >= 0`` check is replaced by ``positions_short_guard``.

The downgrade refuses while a short position exists.

Revision ID: 0022_cfd_shorts
Revises: 0021_gold_hfm_decisions
Create Date: 2026-11-05
"""

from cosmic.db.sqlsplit import run_script

revision = "0022_cfd_shorts"
down_revision = "0021_gold_hfm_decisions"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    ALTER TABLE positions DROP CONSTRAINT positions_quantity_check;
    CREATE FUNCTION positions_short_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.quantity < 0 AND NOT EXISTS (
        SELECT 1
        FROM instruments i, accounts a
        WHERE i.id = NEW.instrument_id
          AND a.id = NEW.account_id
          AND i.asset_class = 'CFD'
          AND a.kind IN ('PAPER', 'SANDBOX')
      ) THEN
        RAISE EXCEPTION 'a short position is only possible on a CFD of a simulated account'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER positions_short_guard BEFORE INSERT OR UPDATE ON positions
      FOR EACH ROW EXECUTE FUNCTION positions_short_guard();
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM positions WHERE quantity < 0) THEN
        RAISE EXCEPTION 'short positions exist; the schema before 0022 cannot hold them';
      END IF;
    END $$;
    DROP TRIGGER positions_short_guard ON positions;
    DROP FUNCTION positions_short_guard();
    ALTER TABLE positions ADD CONSTRAINT positions_quantity_check CHECK (quantity >= 0);
    """
    )
