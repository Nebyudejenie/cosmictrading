"""The HFM demo self-test (S67, ADR-037 addendum): one buy and one short of HFM's smallest gold
trade under the owner's mandate, each with its stop-loss verified at HFM and closed automatically
(its take-profit, or a time limit), then a reconciliation; the evidence is kept.

- ``gold_hfm_selftests``: one row per run; its phase, the two trades it proposed, its outcome
  and the evidence (HFM's deal and position numbers). The row advances as the run does.

The downgrade refuses while a run exists.

Revision ID: 0023_gold_hfm_selftests
Revises: 0022_cfd_shorts
Create Date: 2026-11-05
"""

from cosmic.db.sqlsplit import run_script

revision = "0023_gold_hfm_selftests"
down_revision = "0022_cfd_shorts"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        f"""
    CREATE TABLE gold_hfm_selftests (
      id uuid PRIMARY KEY,
      mandate_id uuid NOT NULL REFERENCES trading_mandates(id),
      requested_by uuid NOT NULL REFERENCES users(id),
      requested_at timestamptz NOT NULL,
      phase text NOT NULL CHECK (phase IN ('BUY','SELL','RECONCILE','DONE')),
      buy_intent uuid REFERENCES trade_intents(id),
      sell_intent uuid REFERENCES trade_intents(id),
      outcome text CHECK (outcome IS NULL OR outcome IN ('PASSED','FAILED')),
      evidence jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      updated_at timestamptz NOT NULL,
      finished_at timestamptz
    );
    CREATE INDEX gold_hfm_selftests_open ON gold_hfm_selftests (mandate_id)
      WHERE finished_at IS NULL;
    REVOKE DELETE, TRUNCATE ON gold_hfm_selftests FROM {app};
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM gold_hfm_selftests) THEN
        RAISE EXCEPTION 'self-test runs exist; the schema before 0023 cannot hold them';
      END IF;
    END $$;
    DROP TABLE gold_hfm_selftests;
    """
    )
