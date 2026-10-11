"""Trade intents, approval packets/challenges/approvals, orders, fills, reconciliation,
recovery checklists, paper venue books, post-trade reviews, Telegram link codes.

Revision ID: 0004_execution
Revises: 0003_trading_core
Create Date: 2026-10-11
"""

from cosmic.db.sqlsplit import run_script

revision = "0004_execution"
down_revision = "0003_trading_core"
branch_labels = None
depends_on = None

ORDER_STATES = (
    "('CREATED','VALIDATED','AUTHORIZED','SUBMITTING','SUBMITTED','ACKNOWLEDGED','PARTIALLY_FILLED',"
    "'FILLED','CANCEL_PENDING','CANCELLED','REJECTED','EXPIRED','REPLACE_PENDING','UNKNOWN_ORDER_STATE')"
)
INTENT_STATES = (
    "('NON_EXECUTABLE','PROPOSED','APPROVED','REJECTED','CHANGES_REQUESTED','EXPIRED','REVOKED',"
    "'REQUIRES_REAPPROVAL','EXECUTING','EXECUTED','EXECUTION_FAILED','BLOCKED','CANCELLED')"
)


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(f"""
    CREATE TABLE trade_intents (
      id uuid PRIMARY KEY,
      trade_code text NOT NULL UNIQUE,
      account_id uuid NOT NULL REFERENCES accounts(id),
      instrument_id uuid NOT NULL REFERENCES instruments(id),
      opportunity_id uuid,
      side text NOT NULL CHECK (side IN ('BUY','SELL')),
      effect text NOT NULL CHECK (effect IN ('OPEN','INCREASE','REDUCE','CLOSE')),
      quantity numeric(38,18) NOT NULL CHECK (quantity > 0),
      order_type text NOT NULL,
      limit_price numeric(38,18) NOT NULL CHECK (limit_price > 0),
      price_band_low numeric(38,18) NOT NULL,
      price_band_high numeric(38,18) NOT NULL,
      max_slippage_bps numeric(38,18) NOT NULL CHECK (max_slippage_bps >= 0),
      time_in_force text NOT NULL,
      management_plan jsonb NOT NULL,
      thesis text NOT NULL,
      plain_summary text NOT NULL,
      horizon text NOT NULL,
      details jsonb NOT NULL,
      intent_expires_at timestamptz NOT NULL,
      created_by_kind text NOT NULL CHECK (created_by_kind IN ('USER','WORKER')),
      created_by text NOT NULL,
      status text NOT NULL CHECK (status IN {INTENT_STATES}),
      status_reason text,
      is_simulated boolean NOT NULL,
      created_at timestamptz NOT NULL,
      updated_at timestamptz NOT NULL,
      CHECK (price_band_low <= limit_price AND limit_price <= price_band_high)
    );
    CREATE INDEX trade_intents_status ON trade_intents (status);

    CREATE FUNCTION trade_intents_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (to_jsonb(NEW) - 'status' - 'status_reason' - 'updated_at')
         IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'status_reason' - 'updated_at') THEN
        RAISE EXCEPTION 'trade intent material terms are immutable; create a new intent'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER trade_intents_immutable_terms BEFORE UPDATE ON trade_intents
      FOR EACH ROW EXECUTE FUNCTION trade_intents_guard();
    CREATE TRIGGER trade_intents_no_delete BEFORE DELETE ON trade_intents
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

    CREATE TABLE approval_packets (
      id uuid PRIMARY KEY,
      intent_id uuid NOT NULL REFERENCES trade_intents(id),
      version integer NOT NULL CHECK (version >= 1),
      packet jsonb NOT NULL,
      packet_hash text NOT NULL UNIQUE,
      risk_check_run_id uuid NOT NULL REFERENCES risk_check_runs(id),
      policy_version integer NOT NULL,
      created_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      UNIQUE (intent_id, version)
    );
    CREATE TRIGGER approval_packets_immutable BEFORE UPDATE OR DELETE ON approval_packets
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON approval_packets FROM {app};

    CREATE TABLE approval_challenges (
      id uuid PRIMARY KEY,
      token_hash bytea NOT NULL UNIQUE,
      intent_id uuid NOT NULL REFERENCES trade_intents(id),
      packet_id uuid NOT NULL REFERENCES approval_packets(id),
      packet_hash text NOT NULL,
      user_id uuid NOT NULL REFERENCES users(id),
      channel text NOT NULL CHECK (channel IN ('PORTAL','TELEGRAM')),
      created_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      used_at timestamptz
    );

    CREATE TABLE approvals (
      id uuid PRIMARY KEY,
      intent_id uuid NOT NULL REFERENCES trade_intents(id),
      packet_id uuid NOT NULL UNIQUE REFERENCES approval_packets(id),
      packet_hash text NOT NULL,
      decision text NOT NULL CHECK (decision IN ('APPROVED','REJECTED','CHANGES_REQUESTED')),
      operator_user_id uuid NOT NULL REFERENCES users(id),
      channel text NOT NULL CHECK (channel IN ('PORTAL','TELEGRAM')),
      nonce text NOT NULL UNIQUE,
      policy_version integer NOT NULL,
      decided_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      status text NOT NULL CHECK (status IN ('ACTIVE','CONSUMED','EXPIRED','REVOKED','SUPERSEDED','FINAL')),
      consumed_at timestamptz,
      consumed_order_id uuid,
      status_reason text,
      note text,
      CHECK (decision = 'APPROVED' OR status = 'FINAL')
    );
    CREATE INDEX approvals_intent ON approvals (intent_id);

    CREATE TABLE orders (
      id uuid PRIMARY KEY,
      intent_id uuid NOT NULL REFERENCES trade_intents(id),
      approval_id uuid REFERENCES approvals(id),
      parent_order_id uuid REFERENCES orders(id),
      purpose text NOT NULL CHECK (purpose IN ('ENTRY','STOP_LOSS','TAKE_PROFIT','EXIT')),
      account_id uuid NOT NULL REFERENCES accounts(id),
      instrument_id uuid NOT NULL REFERENCES instruments(id),
      client_order_id text NOT NULL UNIQUE,
      broker_order_id text,
      side text NOT NULL CHECK (side IN ('BUY','SELL')),
      order_type text NOT NULL CHECK (order_type IN ('LIMIT','STOP_LIMIT')),
      quantity numeric(38,18) NOT NULL CHECK (quantity > 0),
      limit_price numeric(38,18) NOT NULL CHECK (limit_price > 0),
      stop_price numeric(38,18),
      time_in_force text NOT NULL,
      expires_at timestamptz,
      status text NOT NULL CHECK (status IN {ORDER_STATES}),
      status_version integer NOT NULL DEFAULT 1,
      filled_qty numeric(38,18) NOT NULL DEFAULT 0,
      avg_fill_price numeric(38,18),
      fees_paid numeric(38,18) NOT NULL DEFAULT 0,
      fee_currency text,
      authority text NOT NULL CHECK (authority IN ('APPROVED_EXECUTION','PROTECTIVE_MANAGEMENT')),
      reduce_only boolean NOT NULL,
      is_simulated boolean NOT NULL,
      created_at timestamptz NOT NULL,
      updated_at timestamptz NOT NULL,
      submitted_at timestamptz,
      acknowledged_at timestamptz,
      last_broker_sync_at timestamptz,
      unknown_since timestamptz,
      last_error text,
      CHECK (purpose = 'ENTRY' OR reduce_only),
      CHECK (filled_qty >= 0 AND filled_qty <= quantity),
      CHECK (order_type <> 'STOP_LIMIT' OR stop_price IS NOT NULL)
    );
    CREATE UNIQUE INDEX orders_one_entry_per_intent ON orders (intent_id) WHERE purpose = 'ENTRY';
    CREATE INDEX orders_account_status ON orders (account_id, status);

    CREATE TABLE order_events (
      id uuid PRIMARY KEY,
      order_id uuid NOT NULL REFERENCES orders(id),
      seq integer NOT NULL,
      from_status text,
      to_status text NOT NULL,
      source text NOT NULL CHECK (source IN ('LOCAL','BROKER','RECONCILE')),
      detail jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      occurred_at timestamptz NOT NULL,
      UNIQUE (order_id, seq)
    );
    CREATE TRIGGER order_events_immutable BEFORE UPDATE OR DELETE ON order_events
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON order_events FROM {app};

    CREATE TABLE fills (
      id uuid PRIMARY KEY,
      order_id uuid NOT NULL REFERENCES orders(id),
      broker_fill_id text NOT NULL,
      quantity numeric(38,18) NOT NULL CHECK (quantity > 0),
      price numeric(38,18) NOT NULL CHECK (price > 0),
      fee numeric(38,18) NOT NULL CHECK (fee >= 0),
      fee_currency text NOT NULL,
      liquidity text,
      realized_pnl numeric(38,18),
      filled_at timestamptz NOT NULL,
      source text NOT NULL CHECK (source IN ('BROKER','RECONCILE')),
      is_simulated boolean NOT NULL,
      recorded_at timestamptz NOT NULL,
      UNIQUE (order_id, broker_fill_id)
    );
    CREATE INDEX fills_filled_at ON fills (filled_at);
    CREATE TRIGGER fills_immutable BEFORE UPDATE OR DELETE ON fills
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON fills FROM {app};

    ALTER TABLE risk_check_runs ADD CONSTRAINT risk_check_runs_intent_fk
      FOREIGN KEY (intent_id) REFERENCES trade_intents(id);
    ALTER TABLE risk_check_runs ADD CONSTRAINT risk_check_runs_order_fk
      FOREIGN KEY (order_id) REFERENCES orders(id);

    CREATE TABLE reconciliation_runs (
      id uuid PRIMARY KEY,
      account_id uuid NOT NULL REFERENCES accounts(id),
      trigger text NOT NULL,
      started_at timestamptz NOT NULL,
      finished_at timestamptz,
      status text NOT NULL CHECK (status IN ('RUNNING','PASSED','FAILED')),
      summary jsonb NOT NULL DEFAULT '{{}}'::jsonb
    );
    CREATE INDEX reconciliation_runs_account ON reconciliation_runs (account_id, started_at);
    CREATE TABLE reconciliation_items (
      id uuid PRIMARY KEY,
      run_id uuid NOT NULL REFERENCES reconciliation_runs(id),
      step text NOT NULL CHECK (step IN ('BROKER_ORDERS','FILLS','POSITIONS','BALANCES','LEDGER','ACCOUNT_STATE')),
      subject text NOT NULL,
      internal_value text,
      broker_value text,
      status text NOT NULL CHECK (status IN ('MATCH','MISMATCH','IMPORTED','RESOLVED')),
      detail jsonb NOT NULL DEFAULT '{{}}'::jsonb
    );
    CREATE INDEX reconciliation_items_run ON reconciliation_items (run_id);

    CREATE TABLE recovery_checklists (
      id uuid PRIMARY KEY,
      opened_at timestamptz NOT NULL,
      opened_by text NOT NULL,
      stop_reason text NOT NULL,
      stop_version integer NOT NULL,
      status text NOT NULL CHECK (status IN ('OPEN','COMPLETED')),
      completed_at timestamptz,
      completed_by uuid REFERENCES users(id),
      resume_mode text,
      resume_reason text,
      evidence jsonb NOT NULL DEFAULT '{{}}'::jsonb
    );
    CREATE UNIQUE INDEX recovery_checklists_one_open ON recovery_checklists (status) WHERE status = 'OPEN';

    CREATE TABLE paper_balances (
      account_ref uuid NOT NULL REFERENCES accounts(id),
      asset text NOT NULL,
      total numeric(38,18) NOT NULL,
      hold numeric(38,18) NOT NULL DEFAULT 0,
      updated_at timestamptz NOT NULL,
      PRIMARY KEY (account_ref, asset),
      CHECK (total >= 0 AND hold >= 0 AND hold <= total)
    );
    CREATE TABLE paper_orders (
      id uuid PRIMARY KEY,
      account_ref uuid NOT NULL REFERENCES accounts(id),
      client_order_id text NOT NULL UNIQUE,
      venue_order_id text NOT NULL UNIQUE,
      instrument_key text NOT NULL,
      side text NOT NULL,
      order_type text NOT NULL,
      quantity numeric(38,18) NOT NULL,
      limit_price numeric(38,18) NOT NULL,
      stop_price numeric(38,18),
      triggered boolean NOT NULL DEFAULT false,
      time_in_force text NOT NULL,
      expires_at timestamptz,
      status text NOT NULL CHECK (status IN ('OPEN','PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED','EXPIRED')),
      filled_qty numeric(38,18) NOT NULL DEFAULT 0,
      hold_asset text,
      hold_amount numeric(38,18) NOT NULL DEFAULT 0,
      reject_reason text,
      created_at timestamptz NOT NULL,
      updated_at timestamptz NOT NULL
    );
    CREATE TABLE paper_fills (
      id uuid PRIMARY KEY,
      paper_order_id uuid NOT NULL REFERENCES paper_orders(id),
      venue_fill_id text NOT NULL UNIQUE,
      quantity numeric(38,18) NOT NULL,
      price numeric(38,18) NOT NULL,
      fee numeric(38,18) NOT NULL,
      fee_asset text NOT NULL,
      liquidity text NOT NULL,
      filled_at timestamptz NOT NULL
    );
    CREATE TABLE paper_faults (
      id uuid PRIMARY KEY,
      fault text NOT NULL CHECK (fault IN ('REJECT_NEXT','TIMEOUT_AFTER_ACCEPT','TIMEOUT_BEFORE_ACCEPT',
        'UNAVAILABLE','DUPLICATE_FILL_REPORT','LOSE_CANCEL_RESPONSE')),
      remaining integer NOT NULL CHECK (remaining >= 0),
      created_at timestamptz NOT NULL
    );

    CREATE TABLE post_trade_reviews (
      id uuid PRIMARY KEY,
      intent_id uuid NOT NULL UNIQUE REFERENCES trade_intents(id),
      created_at timestamptz NOT NULL,
      planned jsonb NOT NULL,
      actual jsonb NOT NULL,
      assessment jsonb NOT NULL,
      realized_pnl numeric(38,18),
      is_simulated boolean NOT NULL
    );

    CREATE TABLE telegram_link_codes (
      id uuid PRIMARY KEY,
      code_hash bytea NOT NULL UNIQUE,
      user_id uuid NOT NULL REFERENCES users(id),
      created_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      used_at timestamptz
    );
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DROP TABLE telegram_link_codes;
    DROP TABLE post_trade_reviews;
    DROP TABLE paper_faults;
    DROP TABLE paper_fills;
    DROP TABLE paper_orders;
    DROP TABLE paper_balances;
    DROP TABLE recovery_checklists;
    DROP TABLE reconciliation_items;
    DROP TABLE reconciliation_runs;
    ALTER TABLE risk_check_runs DROP CONSTRAINT risk_check_runs_order_fk;
    ALTER TABLE risk_check_runs DROP CONSTRAINT risk_check_runs_intent_fk;
    DROP TABLE fills;
    DROP TABLE order_events;
    DROP TABLE orders;
    DROP TABLE approvals;
    DROP TABLE approval_challenges;
    DROP TABLE approval_packets;
    DROP TABLE trade_intents;
    DROP FUNCTION trade_intents_guard();
    """)
