"""Authority state, accounts, risk policies and checks, double-entry ledger, positions, go-live.

Revision ID: 0003_trading_core
Revises: 0002_market_data
Create Date: 2026-10-11
"""

from cosmic.db.sqlsplit import run_script

revision = "0003_trading_core"
down_revision = "0002_market_data"
branch_labels = None
depends_on = None

MODES = "('RESEARCH_ONLY','PAPER_TRADING','APPROVAL_REQUIRED','STRATEGY_AUTHORIZED','PROTECTION_ONLY','EMERGENCY_STOP')"


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(f"""
    CREATE TABLE authority_state (
      id smallint PRIMARY KEY CHECK (id = 1),
      mode text NOT NULL CHECK (mode IN {MODES}),
      version integer NOT NULL CHECK (version >= 1),
      mode_before_stop text CHECK (mode_before_stop IN {MODES}),
      changed_at timestamptz NOT NULL,
      changed_by text NOT NULL,
      reason text NOT NULL,
      live_activation_id uuid
    );
    INSERT INTO authority_state (id, mode, version, changed_at, changed_by, reason)
      VALUES (1, 'RESEARCH_ONLY', 1, now(), 'migration', 'initial safe default');
    REVOKE INSERT, DELETE, TRUNCATE ON authority_state FROM {app};

    CREATE TABLE authority_transitions (
      id uuid PRIMARY KEY,
      from_mode text NOT NULL,
      to_mode text NOT NULL,
      version integer NOT NULL,
      actor_type text NOT NULL,
      actor_id text,
      reason text NOT NULL,
      preconditions jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      occurred_at timestamptz NOT NULL
    );
    CREATE TRIGGER authority_transitions_immutable BEFORE UPDATE OR DELETE ON authority_transitions
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON authority_transitions FROM {app};

    CREATE TABLE accounts (
      id uuid PRIMARY KEY,
      alias text NOT NULL UNIQUE CHECK (alias ~ '^[a-z0-9][a-z0-9-]{{1,40}}$'),
      kind text NOT NULL CHECK (kind IN ('PAPER','LIVE')),
      venue_id uuid NOT NULL REFERENCES venues(id),
      broker_adapter text NOT NULL,
      base_currency text NOT NULL,
      status text NOT NULL CHECK (status IN ('ACTIVE','DISABLED')),
      reconciliation_status text NOT NULL CHECK (reconciliation_status IN ('OK','REQUIRED','FAILED','UNKNOWN')),
      last_reconciled_at timestamptz,
      created_at timestamptz NOT NULL,
      updated_at timestamptz NOT NULL,
      CHECK (kind = 'LIVE' OR broker_adapter = 'paper')
    );

    CREATE TABLE risk_policies (
      id uuid PRIMARY KEY,
      version integer NOT NULL UNIQUE CHECK (version >= 1),
      params jsonb NOT NULL,
      params_hash text NOT NULL,
      active boolean NOT NULL,
      reason text NOT NULL,
      created_by text NOT NULL,
      created_at timestamptz NOT NULL
    );
    CREATE UNIQUE INDEX risk_policies_one_active ON risk_policies (active) WHERE active;
    REVOKE DELETE, TRUNCATE ON risk_policies FROM {app};

    CREATE FUNCTION risk_policies_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      -- Policy content is immutable; only the active flag may change (new versions are inserted).
      IF NEW.params IS DISTINCT FROM OLD.params OR NEW.params_hash IS DISTINCT FROM OLD.params_hash
         OR NEW.version IS DISTINCT FROM OLD.version OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'risk policy versions are immutable' USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER risk_policies_immutable BEFORE UPDATE ON risk_policies
      FOR EACH ROW EXECUTE FUNCTION risk_policies_guard();
    CREATE TRIGGER risk_policies_no_delete BEFORE DELETE ON risk_policies
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

    CREATE TABLE risk_check_runs (
      id uuid PRIMARY KEY,
      account_id uuid REFERENCES accounts(id),
      intent_id uuid,
      order_id uuid,
      stage text NOT NULL CHECK (stage IN ('PRE_APPROVAL','PRE_SUBMISSION','PROTECTIVE')),
      policy_version integer NOT NULL,
      decision text NOT NULL CHECK (decision IN ('PASS','FAIL')),
      exposure_increasing boolean NOT NULL,
      results jsonb NOT NULL,
      context jsonb NOT NULL,
      created_at timestamptz NOT NULL
    );
    CREATE INDEX risk_check_runs_intent ON risk_check_runs (intent_id);
    CREATE TRIGGER risk_check_runs_immutable BEFORE UPDATE OR DELETE ON risk_check_runs
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON risk_check_runs FROM {app};

    CREATE TABLE ledger_txns (
      id uuid PRIMARY KEY,
      account_id uuid NOT NULL REFERENCES accounts(id),
      kind text NOT NULL CHECK (kind IN ('CAPITAL','FILL','FEE','ADJUSTMENT_RECONCILE')),
      ref_type text,
      ref_id text,
      description text NOT NULL,
      is_simulated boolean NOT NULL,
      created_at timestamptz NOT NULL,
      UNIQUE (kind, ref_type, ref_id)
    );
    CREATE TABLE ledger_entries (
      id uuid PRIMARY KEY,
      txn_id uuid NOT NULL REFERENCES ledger_txns(id),
      account_id uuid NOT NULL REFERENCES accounts(id),
      book text NOT NULL CHECK (book IN ('CASH','POSITION','CLEARING','FEES','CAPITAL')),
      asset text NOT NULL,
      amount numeric(38,18) NOT NULL CHECK (amount <> 0),
      created_at timestamptz NOT NULL
    );
    CREATE INDEX ledger_entries_account ON ledger_entries (account_id, book, asset);
    CREATE INDEX ledger_entries_txn ON ledger_entries (txn_id);

    CREATE FUNCTION ledger_txn_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      bad record;
    BEGIN
      SELECT asset, sum(amount) AS total INTO bad FROM ledger_entries
        WHERE txn_id = NEW.txn_id GROUP BY asset HAVING sum(amount) <> 0 LIMIT 1;
      IF FOUND THEN
        RAISE EXCEPTION 'ledger transaction % does not balance for asset % (sum %)',
          NEW.txn_id, bad.asset, bad.total USING ERRCODE = 'check_violation';
      END IF;
      RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER ledger_entries_balanced AFTER INSERT ON ledger_entries
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger_txn_balanced();
    CREATE TRIGGER ledger_entries_immutable BEFORE UPDATE OR DELETE ON ledger_entries
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    CREATE TRIGGER ledger_txns_immutable BEFORE UPDATE OR DELETE ON ledger_txns
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON ledger_entries, ledger_txns FROM {app};

    CREATE TABLE positions (
      account_id uuid NOT NULL REFERENCES accounts(id),
      instrument_id uuid NOT NULL REFERENCES instruments(id),
      quantity numeric(38,18) NOT NULL,
      avg_cost numeric(38,18),
      realized_pnl numeric(38,18) NOT NULL DEFAULT 0,
      protection_state text NOT NULL DEFAULT 'NONE'
        CHECK (protection_state IN ('NONE','PENDING','ACTIVE','DEGRADED','UNKNOWN')),
      updated_at timestamptz NOT NULL,
      PRIMARY KEY (account_id, instrument_id),
      CHECK (quantity >= 0)
    );

    CREATE TABLE equity_snapshots (
      id uuid PRIMARY KEY,
      account_id uuid NOT NULL REFERENCES accounts(id),
      taken_at timestamptz NOT NULL,
      equity numeric(38,18) NOT NULL,
      cash numeric(38,18) NOT NULL,
      positions_value numeric(38,18) NOT NULL,
      price_label text NOT NULL,
      is_simulated boolean NOT NULL
    );
    CREATE INDEX equity_snapshots_account ON equity_snapshots (account_id, taken_at);

    CREATE TABLE golive_gates (
      id uuid PRIMARY KEY,
      gate_key text NOT NULL CHECK (gate_key IN ('A','B','C','D','E','F','G','H','I')),
      status text NOT NULL CHECK (status IN ('PASSED','FAILED','NOT_MET')),
      evidence jsonb NOT NULL,
      recorded_by text NOT NULL,
      recorded_at timestamptz NOT NULL
    );
    CREATE TRIGGER golive_gates_immutable BEFORE UPDATE OR DELETE ON golive_gates
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON golive_gates FROM {app};

    CREATE TABLE live_activations (
      id uuid PRIMARY KEY,
      activated_by uuid NOT NULL REFERENCES users(id),
      readiness_snapshot jsonb NOT NULL,
      activated_at timestamptz NOT NULL,
      revoked_at timestamptz
    );
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DROP TABLE live_activations;
    DROP TABLE golive_gates;
    DROP TABLE equity_snapshots;
    DROP TABLE positions;
    DROP TABLE ledger_entries;
    DROP TABLE ledger_txns;
    DROP FUNCTION ledger_txn_balanced();
    DROP TABLE risk_check_runs;
    DROP TABLE risk_policies;
    DROP FUNCTION risk_policies_guard();
    DROP TABLE accounts;
    DROP TABLE authority_transitions;
    DROP TABLE authority_state;
    """)
