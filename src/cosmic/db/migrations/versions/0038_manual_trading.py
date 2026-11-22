"""Manual Trading (S113, the owner's specification of 2026-11-22).

- ``manual_risk_settings``: the owner's own risk rules for the trades they place by hand on
  their real account, one version per change, never edited: the latest version counts. Risk a
  trade is at most 10 % of equity whatever is written (the owner's PDF plan's own maximum); the
  owner's specification sets 1 % by default and 2 % at most, changed only with their code.
- ``manual_journal``: what the owner decided at each trade preview: PLACED (they placed it in
  MT5), SKIPPED (with why) or a NOTE, with the prices, size, risk, Trader Insight's reading and
  the checks as shown. Never edited or deleted; ``request_key`` refuses a double submission.

Neither table can place, change or close an order: they are records. The downgrade refuses
while either holds a row.

Revision ID: 0038_manual_trading
Revises: 0037_robot_starts
Create Date: 2026-11-22
"""

from cosmic.db.sqlsplit import run_script

revision = "0038_manual_trading"
down_revision = "0037_robot_starts"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        f"""
    CREATE TABLE manual_risk_settings (
      version integer PRIMARY KEY CHECK (version > 0),
      params jsonb NOT NULL,
      reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
      created_by text NOT NULL,
      created_at timestamptz NOT NULL,
      CHECK ((params->>'risk_pct_max')::numeric > 0
             AND (params->>'risk_pct_max')::numeric <= 10),
      CHECK ((params->>'risk_pct_default')::numeric > 0
             AND (params->>'risk_pct_default')::numeric
                 <= (params->>'risk_pct_max')::numeric)
    );
    CREATE TABLE manual_journal (
      id uuid PRIMARY KEY,
      owner_id uuid NOT NULL REFERENCES users (id),
      created_at timestamptz NOT NULL,
      kind text NOT NULL CHECK (kind IN ('PLACED','SKIPPED','NOTE')),
      symbol text NOT NULL CHECK (length(symbol) BETWEEN 1 AND 32),
      side text CHECK (side IN ('BUY','SELL')),
      entry numeric(38,18),
      stop numeric(38,18),
      target numeric(38,18),
      lots numeric(38,18),
      risk_cash numeric(38,18),
      risk_pct numeric(38,18),
      reward_cash numeric(38,18),
      currency text,
      account_fingerprint text,
      insight jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      checks jsonb NOT NULL DEFAULT '[]'::jsonb,
      note text CHECK (note IS NULL OR length(note) <= 1000),
      request_key text NOT NULL,
      CHECK (kind = 'NOTE' OR side IS NOT NULL),
      CONSTRAINT manual_journal_request_key UNIQUE (request_key)
    );
    CREATE INDEX manual_journal_created ON manual_journal (created_at);
    CREATE FUNCTION manual_records_sealed() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'manual trading records are append-only';
    END $$;
    CREATE TRIGGER manual_risk_settings_sealed BEFORE UPDATE OR DELETE ON manual_risk_settings
      FOR EACH ROW EXECUTE FUNCTION manual_records_sealed();
    CREATE TRIGGER manual_journal_sealed BEFORE UPDATE OR DELETE ON manual_journal
      FOR EACH ROW EXECUTE FUNCTION manual_records_sealed();
    REVOKE UPDATE, DELETE, TRUNCATE ON manual_risk_settings FROM {app};
    REVOKE UPDATE, DELETE, TRUNCATE ON manual_journal FROM {app};
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM manual_risk_settings) OR EXISTS (SELECT 1 FROM manual_journal)
      THEN
        RAISE EXCEPTION 'manual trading records exist: they are kept, so 0038 is not undone';
      END IF;
    END $$;
    DROP TABLE manual_journal;
    DROP TABLE manual_risk_settings;
    DROP FUNCTION manual_records_sealed();
    """
    )
