"""My Gold Auto-Trader on the HFM demo (S64, ADR-037 addendum): every decision of the gold
reading under its mandate, once per 1-minute candle.

- ``gold_hfm_decisions``: append-only; the unique key (mandate, candle) is what keeps a restart
  from deciding, or proposing, twice. A proposal it made is named by its trade intent.

The downgrade refuses while a decision exists.

Revision ID: 0021_gold_hfm_decisions
Revises: 0020_gold_simulation
Create Date: 2026-11-04
"""

from cosmic.db.sqlsplit import run_script

revision = "0021_gold_hfm_decisions"
down_revision = "0020_gold_simulation"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        f"""
    CREATE TABLE gold_hfm_decisions (
      id uuid PRIMARY KEY,
      mandate_id uuid NOT NULL REFERENCES trading_mandates(id),
      bar timestamptz NOT NULL,
      at timestamptz NOT NULL,
      decision text NOT NULL CHECK (decision IN ('BUY','SELL','WAIT')),
      action text NOT NULL CHECK (action IN ('WAIT','PROPOSED','REFUSED','EXIT')),
      reasons jsonb NOT NULL,
      bid numeric(38,18),
      ask numeric(38,18),
      ounces numeric(38,18) CHECK (ounces IS NULL OR ounces > 0),
      stop_distance numeric(38,18) CHECK (stop_distance IS NULL OR stop_distance > 0),
      intent_id uuid REFERENCES trade_intents(id),
      UNIQUE (mandate_id, bar)
    );
    CREATE INDEX gold_hfm_decisions_at ON gold_hfm_decisions (mandate_id, at);
    CREATE TRIGGER gold_hfm_decisions_immutable BEFORE UPDATE OR DELETE
      ON gold_hfm_decisions FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON gold_hfm_decisions FROM {app};
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM gold_hfm_decisions) THEN
        RAISE EXCEPTION 'gold reading decisions exist; the schema before 0021 cannot hold them';
      END IF;
    END $$;
    DROP TABLE gold_hfm_decisions;
    """
    )
