"""Demo mandates (ADR-030): an owner-granted, bounded standing approval for simulated accounts.

- ``trading_mandates``: what the auto-trader may do on one account (rule, timeframe,
  instruments, maximum loss per trade, trades per day, open positions, daily loss, expiry).
  At most one ACTIVE mandate per account. A trigger refuses any account that is not PAPER or
  SANDBOX, so no code path can give real money to the auto-trader. Terms never change; a
  mandate ends by revocation or expiry and stays ended. Rows are never deleted.
- ``approvals``: a new channel MANDATE, which must name its mandate. A trigger refuses a
  MANDATE approval unless that mandate is ACTIVE, unexpired and covers the trade's account.

The downgrade refuses while mandates exist (nothing is deleted).

Revision ID: 0011_demo_mandates
Revises: 0010_hfm_bridge
Create Date: 2026-10-20
"""

from cosmic.db.sqlsplit import run_script

revision = "0011_demo_mandates"
down_revision = "0010_hfm_bridge"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(f"""
    CREATE TABLE trading_mandates (
      id uuid PRIMARY KEY,
      account_id uuid NOT NULL REFERENCES accounts(id),
      strategy_key text NOT NULL,
      timeframe text NOT NULL CHECK (timeframe IN ('1h')),
      instruments text[] NOT NULL CHECK (cardinality(instruments) BETWEEN 1 AND 5),
      max_loss_per_trade numeric(38,18) NOT NULL CHECK (max_loss_per_trade > 0),
      max_trades_per_day integer NOT NULL CHECK (max_trades_per_day BETWEEN 1 AND 20),
      max_open_positions integer NOT NULL CHECK (max_open_positions BETWEEN 1 AND 5),
      daily_loss_limit numeric(38,18) NOT NULL CHECK (daily_loss_limit > 0),
      status text NOT NULL CHECK (status IN ('ACTIVE','REVOKED','EXPIRED')),
      granted_by uuid NOT NULL REFERENCES users(id),
      granted_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      ended_at timestamptz,
      ended_by text,
      end_reason text,
      CHECK (expires_at > granted_at),
      CHECK ((status = 'ACTIVE') = (ended_at IS NULL))
    );
    CREATE UNIQUE INDEX trading_mandates_one_active ON trading_mandates (account_id)
      WHERE status = 'ACTIVE';

    CREATE FUNCTION trading_mandates_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM accounts WHERE id = NEW.account_id AND kind IN ('PAPER','SANDBOX')
      ) THEN
        RAISE EXCEPTION 'a mandate is allowed only on a PAPER or SANDBOX account'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      IF TG_OP = 'UPDATE' THEN
        IF OLD.status <> 'ACTIVE' THEN
          RAISE EXCEPTION 'an ended mandate stays ended' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF (to_jsonb(NEW) - 'status' - 'ended_at' - 'ended_by' - 'end_reason')
           IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'ended_at' - 'ended_by' - 'end_reason')
        THEN
          RAISE EXCEPTION 'mandate terms are immutable; grant a new mandate'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER trading_mandates_guard BEFORE INSERT OR UPDATE ON trading_mandates
      FOR EACH ROW EXECUTE FUNCTION trading_mandates_guard();
    CREATE TRIGGER trading_mandates_no_delete BEFORE DELETE ON trading_mandates
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE DELETE, TRUNCATE ON trading_mandates FROM {app};

    ALTER TABLE approvals ADD COLUMN mandate_id uuid REFERENCES trading_mandates(id);
    ALTER TABLE approvals DROP CONSTRAINT approvals_channel_check;
    ALTER TABLE approvals ADD CONSTRAINT approvals_channel_check
      CHECK (channel IN ('PORTAL','TELEGRAM','MANDATE'));
    ALTER TABLE approvals ADD CONSTRAINT approvals_mandate_check
      CHECK ((channel = 'MANDATE') = (mandate_id IS NOT NULL));

    CREATE FUNCTION approvals_mandate_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.channel = 'MANDATE' AND NOT EXISTS (
        SELECT 1
        FROM trading_mandates m
        JOIN trade_intents t ON t.account_id = m.account_id
        JOIN accounts a ON a.id = m.account_id
        WHERE m.id = NEW.mandate_id AND t.id = NEW.intent_id AND m.status = 'ACTIVE'
          AND m.expires_at > NEW.decided_at AND a.kind IN ('PAPER','SANDBOX')
      ) THEN
        RAISE EXCEPTION 'a mandate approval needs an active mandate for this account'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER approvals_mandate_guard BEFORE INSERT ON approvals
      FOR EACH ROW EXECUTE FUNCTION approvals_mandate_guard();
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM trading_mandates) THEN
        RAISE EXCEPTION 'demo mandates exist; the schema before 0011 cannot hold them';
      END IF;
    END $$;
    DROP TRIGGER approvals_mandate_guard ON approvals;
    DROP FUNCTION approvals_mandate_guard();
    ALTER TABLE approvals DROP CONSTRAINT approvals_mandate_check;
    ALTER TABLE approvals DROP CONSTRAINT approvals_channel_check;
    ALTER TABLE approvals ADD CONSTRAINT approvals_channel_check
      CHECK (channel IN ('PORTAL','TELEGRAM'));
    ALTER TABLE approvals DROP COLUMN mandate_id;
    DROP TABLE trading_mandates;
    DROP FUNCTION trading_mandates_guard();
    """)
