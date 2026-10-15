"""SANDBOX accounts on a venue's test network, UNMANAGED holdings, broker contract runs (ADR-026).

A SANDBOX account trades virtual funds on the Binance Spot Test Network through the
``binance_spot_testnet`` adapter; at most one is active per adapter, because the adapter speaks
for exactly one venue account. Venue holdings the account does not manage are booked as
UNMANAGED, so reconciliation can account for every unit without counting them as equity.
Broker contract runs are evidence and append-only: a SANDBOX account can only be opened when a
PASSED run exists for the key the worker holds.

The downgrade refuses while SANDBOX accounts or UNMANAGED entries exist: the earlier schema
cannot represent them, and ledger rows are never deleted.

Revision ID: 0009_sandbox_accounts
Revises: 0008_strategy_library
Create Date: 2026-10-15
"""

from cosmic.db.sqlsplit import run_script

revision = "0009_sandbox_accounts"
down_revision = "0008_strategy_library"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(f"""
    ALTER TABLE accounts DROP CONSTRAINT accounts_kind_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_kind_check
      CHECK (kind IN ('PAPER','SANDBOX','LIVE'));
    ALTER TABLE accounts DROP CONSTRAINT accounts_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_adapter_check CHECK (
      (kind = 'PAPER' AND broker_adapter = 'paper')
      OR (kind = 'SANDBOX' AND broker_adapter = 'binance_spot_testnet')
      OR kind = 'LIVE'
    );
    CREATE UNIQUE INDEX accounts_one_active_sandbox ON accounts (broker_adapter)
      WHERE kind = 'SANDBOX' AND status = 'ACTIVE';

    ALTER TABLE ledger_entries DROP CONSTRAINT ledger_entries_book_check;
    ALTER TABLE ledger_entries ADD CONSTRAINT ledger_entries_book_check
      CHECK (book IN ('CASH','POSITION','CLEARING','FEES','CAPITAL','UNMANAGED'));

    CREATE TABLE broker_contract_runs (
      id uuid PRIMARY KEY,
      adapter text NOT NULL,
      environment text NOT NULL,
      key_fingerprint text NOT NULL CHECK (key_fingerprint ~ '^[0-9a-f]{{16}}$'),
      status text NOT NULL CHECK (status IN ('PASSED','FAILED')),
      steps jsonb NOT NULL,
      code_version text NOT NULL,
      started_at timestamptz NOT NULL,
      finished_at timestamptz NOT NULL,
      actor_type text NOT NULL,
      actor_id text NOT NULL
    );
    CREATE INDEX broker_contract_runs_key
      ON broker_contract_runs (adapter, key_fingerprint, finished_at);
    CREATE TRIGGER broker_contract_runs_immutable BEFORE UPDATE OR DELETE ON broker_contract_runs
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON broker_contract_runs FROM {app};
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM accounts WHERE kind = 'SANDBOX') THEN
        RAISE EXCEPTION 'SANDBOX accounts exist; the schema before 0009 cannot hold them';
      END IF;
      IF EXISTS (SELECT 1 FROM ledger_entries WHERE book = 'UNMANAGED') THEN
        RAISE EXCEPTION 'UNMANAGED ledger entries exist; the schema before 0009 cannot hold them';
      END IF;
    END $$;
    DROP TABLE broker_contract_runs;
    ALTER TABLE ledger_entries DROP CONSTRAINT ledger_entries_book_check;
    ALTER TABLE ledger_entries ADD CONSTRAINT ledger_entries_book_check
      CHECK (book IN ('CASH','POSITION','CLEARING','FEES','CAPITAL'));
    DROP INDEX accounts_one_active_sandbox;
    ALTER TABLE accounts DROP CONSTRAINT accounts_adapter_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_check
      CHECK (kind = 'LIVE' OR broker_adapter = 'paper');
    ALTER TABLE accounts DROP CONSTRAINT accounts_kind_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_kind_check CHECK (kind IN ('PAPER','LIVE'));
    """)
