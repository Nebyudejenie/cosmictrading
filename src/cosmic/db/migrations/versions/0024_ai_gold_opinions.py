"""The AI's second opinion on HFM's gold (S68, ADR-037 addendum): what a free AI model reads in
HFM's own candles (M1 to D1), recorded with the model that said it. Append-only.

- ``ai_gold_opinions``: one row per opinion; the gold engine reads the latest one and lets it
  stop a trade (WAIT, or the other direction), never start one or loosen a limit.

The downgrade refuses while an opinion exists.

Revision ID: 0024_ai_gold_opinions
Revises: 0023_gold_hfm_selftests
Create Date: 2026-11-05
"""

from cosmic.db.sqlsplit import run_script

revision = "0024_ai_gold_opinions"
down_revision = "0023_gold_hfm_selftests"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        f"""
    CREATE TABLE ai_gold_opinions (
      id uuid PRIMARY KEY,
      at timestamptz NOT NULL,
      bar timestamptz,
      provider_key text NOT NULL,
      model_id text NOT NULL,
      reading text NOT NULL CHECK (reading IN ('BUY','SELL','WAIT')),
      direction text NOT NULL CHECK (direction IN ('UP','DOWN','SIDEWAYS')),
      recommendation text NOT NULL CHECK (recommendation IN ('BUY','SELL','WAIT')),
      confidence text NOT NULL CHECK (confidence IN ('LOW','MEDIUM','HIGH')),
      why text NOT NULL,
      conditions text NOT NULL,
      expected_risk text NOT NULL,
      timing text NOT NULL
    );
    CREATE INDEX ai_gold_opinions_at ON ai_gold_opinions (at);
    CREATE TRIGGER ai_gold_opinions_immutable BEFORE UPDATE OR DELETE
      ON ai_gold_opinions FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON ai_gold_opinions FROM {app};
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM ai_gold_opinions) THEN
        RAISE EXCEPTION 'AI opinions exist; the schema before 0024 cannot hold them';
      END IF;
    END $$;
    DROP TABLE ai_gold_opinions;
    """
    )
