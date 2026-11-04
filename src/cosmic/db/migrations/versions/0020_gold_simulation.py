"""The internal gold simulation (S62, ADR-037): the owner's own starting balance, fractional
ounces, buys and sells, stop-loss and take-profit, on HFM's prices. Never sent to HFM.

- ``gold_sim_accounts``: a simulated account with the owner's starting balance; its state is the
  only thing that changes: RUNNING, PAUSED (automation off: no new trade, the open ones still
  protected) or STOPPED (its trades closed).
- ``gold_sim_settings``: the owner's settings, each change a new row (append-only); the latest
  counts.
- ``gold_sim_positions``: each simulated position; it changes once, from OPEN to CLOSED, and a
  closed one never again.
- ``gold_sim_decisions``: every decision, once per 1-minute candle (append-only; the unique key
  is what keeps a restart from deciding, or opening, twice).
- ``gold_sim_answers``: the owner's answer to a trade the simulation asked about ("Ask me
  first"), once (append-only).
- ``gold_sim_ledger``: every change of the balance (append-only; its key is what keeps a result,
  a swap or a deposit from being booked twice). The balance is its sum.

- ``bridge_commands.kind`` also allows ``RATES``: bridge version 4 sends HFM's own candles on
  request (read-only; S61).

The downgrade refuses while a simulated account or a RATES command exists.

Revision ID: 0020_gold_simulation
Revises: 0019_rehearsal_renewal
Create Date: 2026-11-04
"""

from cosmic.db.sqlsplit import run_script

revision = "0020_gold_simulation"
down_revision = "0019_rehearsal_renewal"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        f"""
    CREATE TABLE gold_sim_accounts (
      id uuid PRIMARY KEY,
      name text NOT NULL UNIQUE CHECK (length(name) BETWEEN 2 AND 40),
      starting_balance numeric(38,18) NOT NULL CHECK (starting_balance > 0),
      currency text NOT NULL CHECK (currency = 'USD'),
      created_by uuid NOT NULL REFERENCES users(id),
      created_at timestamptz NOT NULL,
      status text NOT NULL CHECK (status IN ('RUNNING','PAUSED','STOPPED')),
      status_changed_at timestamptz NOT NULL,
      status_reason text NOT NULL
    );
    CREATE TABLE gold_sim_settings (
      id uuid PRIMARY KEY,
      account_id uuid NOT NULL REFERENCES gold_sim_accounts(id),
      amount numeric(38,18) NOT NULL CHECK (amount > 0),
      risk_pct numeric(38,18) NOT NULL CHECK (risk_pct > 0 AND risk_pct <= 10),
      daily_loss numeric(38,18) NOT NULL CHECK (daily_loss > 0),
      max_open_risk_pct numeric(38,18) NOT NULL
        CHECK (max_open_risk_pct > 0 AND max_open_risk_pct <= 100),
      max_drawdown_pct numeric(38,18) NOT NULL
        CHECK (max_drawdown_pct > 0 AND max_drawdown_pct <= 100),
      max_positions integer NOT NULL CHECK (max_positions BETWEEN 1 AND 10),
      take_profit_r numeric(38,18) CHECK (take_profit_r IS NULL OR take_profit_r > 0),
      allow_sell boolean NOT NULL,
      permission text NOT NULL CHECK (permission IN ('AUTO','ASK')),
      created_by uuid NOT NULL REFERENCES users(id),
      created_at timestamptz NOT NULL
    );
    CREATE INDEX gold_sim_settings_latest ON gold_sim_settings (account_id, created_at);
    CREATE TABLE gold_sim_decisions (
      id uuid PRIMARY KEY,
      account_id uuid NOT NULL REFERENCES gold_sim_accounts(id),
      bar timestamptz NOT NULL,
      at timestamptz NOT NULL,
      decision text NOT NULL CHECK (decision IN ('BUY','SELL','WAIT')),
      action text NOT NULL,
      reasons jsonb NOT NULL,
      bid numeric(38,18),
      ask numeric(38,18),
      ounces numeric(38,18),
      stop_distance numeric(38,18),
      position_id uuid,
      UNIQUE (account_id, bar)
    );
    CREATE TABLE gold_sim_positions (
      id uuid PRIMARY KEY,
      account_id uuid NOT NULL REFERENCES gold_sim_accounts(id),
      decision_id uuid NOT NULL REFERENCES gold_sim_decisions(id) UNIQUE,
      side text NOT NULL CHECK (side IN ('BUY','SELL')),
      ounces numeric(38,18) NOT NULL CHECK (ounces > 0),
      entry_price numeric(38,18) NOT NULL CHECK (entry_price > 0),
      entry_at timestamptz NOT NULL,
      stop_price numeric(38,18) NOT NULL CHECK (stop_price > 0),
      take_profit_price numeric(38,18) CHECK (take_profit_price IS NULL OR take_profit_price > 0),
      spread numeric(38,18) NOT NULL CHECK (spread >= 0),
      margin numeric(38,18) NOT NULL CHECK (margin >= 0),
      status text NOT NULL CHECK (status IN ('OPEN','CLOSED')),
      exit_price numeric(38,18),
      exit_at timestamptz,
      exit_reason text CHECK (exit_reason IS NULL OR exit_reason IN
        ('STOP_LOSS','TAKE_PROFIT','RULE','OWNER')),
      result numeric(38,18),
      CHECK ((status = 'OPEN') = (exit_at IS NULL)),
      CHECK ((status = 'OPEN') = (exit_price IS NULL)),
      CHECK ((status = 'OPEN') = (result IS NULL))
    );
    CREATE INDEX gold_sim_positions_open ON gold_sim_positions (account_id) WHERE status = 'OPEN';
    CREATE TABLE gold_sim_ledger (
      id uuid PRIMARY KEY,
      account_id uuid NOT NULL REFERENCES gold_sim_accounts(id),
      at timestamptz NOT NULL,
      kind text NOT NULL CHECK (kind IN ('DEPOSIT','RESULT','SWAP','COMMISSION')),
      amount numeric(38,18) NOT NULL,
      position_id uuid REFERENCES gold_sim_positions(id),
      ref text NOT NULL UNIQUE,
      note text NOT NULL
    );
    CREATE INDEX gold_sim_ledger_account ON gold_sim_ledger (account_id, at);
    -- The owner's answer to a trade the simulation asked about ("Ask me first"), once.
    CREATE TABLE gold_sim_answers (
      decision_id uuid PRIMARY KEY REFERENCES gold_sim_decisions(id),
      answer text NOT NULL CHECK (answer IN ('APPROVED','REJECTED')),
      answered_by uuid NOT NULL REFERENCES users(id),
      answered_at timestamptz NOT NULL
    );

    -- A closed position never changes again; an open one only closes.
    CREATE FUNCTION gold_sim_positions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.status = 'CLOSED' THEN
        RAISE EXCEPTION 'a closed simulated position never changes'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      IF (to_jsonb(NEW) - 'status' - 'exit_price' - 'exit_at' - 'exit_reason' - 'result')
         IS DISTINCT FROM
         (to_jsonb(OLD) - 'status' - 'exit_price' - 'exit_at' - 'exit_reason' - 'result') THEN
        RAISE EXCEPTION 'a simulated position only closes' USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER gold_sim_positions_guard BEFORE UPDATE ON gold_sim_positions
      FOR EACH ROW EXECUTE FUNCTION gold_sim_positions_guard();
    -- An account's money and identity never change; only whether it runs.
    CREATE FUNCTION gold_sim_accounts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (to_jsonb(NEW) - 'status' - 'status_changed_at' - 'status_reason')
         IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'status_changed_at' - 'status_reason') THEN
        RAISE EXCEPTION 'a simulated account''s starting balance and name never change'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER gold_sim_accounts_guard BEFORE UPDATE ON gold_sim_accounts
      FOR EACH ROW EXECUTE FUNCTION gold_sim_accounts_guard();
    CREATE TRIGGER gold_sim_settings_immutable BEFORE UPDATE OR DELETE
      ON gold_sim_settings FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    CREATE TRIGGER gold_sim_decisions_immutable BEFORE UPDATE OR DELETE
      ON gold_sim_decisions FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    CREATE TRIGGER gold_sim_ledger_immutable BEFORE UPDATE OR DELETE
      ON gold_sim_ledger FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    CREATE TRIGGER gold_sim_answers_immutable BEFORE UPDATE OR DELETE
      ON gold_sim_answers FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    ALTER TABLE bridge_commands DROP CONSTRAINT bridge_commands_kind_check;
    ALTER TABLE bridge_commands ADD CONSTRAINT bridge_commands_kind_check
      CHECK (kind IN ('SUBMIT','CANCEL','PING','RATES'));
    REVOKE DELETE, TRUNCATE ON gold_sim_accounts, gold_sim_positions FROM {app};
    REVOKE UPDATE, DELETE, TRUNCATE ON gold_sim_settings, gold_sim_decisions, gold_sim_ledger,
      gold_sim_answers FROM {app};
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM gold_sim_accounts) THEN
        RAISE EXCEPTION 'simulated gold accounts exist; the schema before 0020 cannot hold them';
      END IF;
      IF EXISTS (SELECT 1 FROM bridge_commands WHERE kind = 'RATES') THEN
        RAISE EXCEPTION 'RATES commands exist; the schema before 0020 cannot hold them';
      END IF;
    END $$;
    ALTER TABLE bridge_commands DROP CONSTRAINT bridge_commands_kind_check;
    ALTER TABLE bridge_commands ADD CONSTRAINT bridge_commands_kind_check
      CHECK (kind IN ('SUBMIT','CANCEL','PING'));
    DROP TABLE gold_sim_answers;
    DROP TABLE gold_sim_ledger;
    DROP TABLE gold_sim_positions;
    DROP TABLE gold_sim_decisions;
    DROP TABLE gold_sim_settings;
    DROP TABLE gold_sim_accounts;
    DROP FUNCTION gold_sim_positions_guard();
    DROP FUNCTION gold_sim_accounts_guard();
    """
    )
