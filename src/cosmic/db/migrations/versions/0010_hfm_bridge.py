"""HFM through a MetaTrader 5 bridge (ADR-028): broker venues, CFDs, bridge state and commands.

- A venue may be a BROKER, an instrument a CFD and an asset a METAL (gold).
- A SANDBOX account may also use the ``hfm_mt5_demo`` adapter (an MT5 demo account).
- ``bridge_state``: the latest snapshot a bridge reported (terminal, account, symbols, ticks,
  positions, orders, deals); one row per bridge, replaced on every sync.
- ``bridge_commands``: trading requests for a bridge and their results. Not evidence (orders,
  fills and the audit trail are); finished commands are pruned after a retention period.

The downgrade refuses while HFM accounts or instruments exist (the earlier schema cannot hold
them); nothing is deleted from accounts, instruments or the ledger.

Revision ID: 0010_hfm_bridge
Revises: 0009_sandbox_accounts
Create Date: 2026-10-15
"""

from cosmic.db.sqlsplit import run_script

revision = "0010_hfm_bridge"
down_revision = "0009_sandbox_accounts"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    ALTER TABLE venues DROP CONSTRAINT venues_kind_check;
    ALTER TABLE venues ADD CONSTRAINT venues_kind_check
      CHECK (kind IN ('EXCHANGE','SIMULATED','BROKER'));
    ALTER TABLE instruments DROP CONSTRAINT instruments_asset_class_check;
    ALTER TABLE instruments ADD CONSTRAINT instruments_asset_class_check
      CHECK (asset_class IN ('CRYPTO_SPOT','CFD'));
    ALTER TABLE assets DROP CONSTRAINT assets_kind_check;
    ALTER TABLE assets ADD CONSTRAINT assets_kind_check CHECK (kind IN ('CRYPTO','FIAT','METAL'));
    ALTER TABLE accounts DROP CONSTRAINT accounts_adapter_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_adapter_check CHECK (
      (kind = 'PAPER' AND broker_adapter = 'paper')
      OR (kind = 'SANDBOX' AND broker_adapter IN ('binance_spot_testnet','hfm_mt5_demo'))
      OR kind = 'LIVE'
    );

    CREATE TABLE bridge_state (
      bridge text PRIMARY KEY,
      received_at timestamptz NOT NULL,
      snapshot jsonb NOT NULL
    );

    CREATE TABLE bridge_commands (
      id uuid PRIMARY KEY,
      bridge text NOT NULL,
      kind text NOT NULL CHECK (kind IN ('SUBMIT','CANCEL','PING')),
      payload jsonb NOT NULL,
      status text NOT NULL CHECK (status IN ('PENDING','SENT','DONE','FAILED','EXPIRED')),
      result jsonb,
      error text,
      created_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      sent_at timestamptz,
      finished_at timestamptz
    );
    CREATE INDEX bridge_commands_queue ON bridge_commands (bridge, status, created_at);
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM accounts WHERE broker_adapter = 'hfm_mt5_demo') THEN
        RAISE EXCEPTION 'HFM accounts exist; the schema before 0010 cannot hold them';
      END IF;
      IF EXISTS (SELECT 1 FROM instruments WHERE asset_class = 'CFD')
         OR EXISTS (SELECT 1 FROM venues WHERE kind = 'BROKER')
         OR EXISTS (SELECT 1 FROM assets WHERE kind = 'METAL') THEN
        RAISE EXCEPTION
          'HFM venues, CFD instruments or metal assets exist; the schema before 0010 cannot hold them';
      END IF;
    END $$;
    DROP TABLE bridge_commands;
    DROP TABLE bridge_state;
    ALTER TABLE accounts DROP CONSTRAINT accounts_adapter_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_adapter_check CHECK (
      (kind = 'PAPER' AND broker_adapter = 'paper')
      OR (kind = 'SANDBOX' AND broker_adapter = 'binance_spot_testnet')
      OR kind = 'LIVE'
    );
    ALTER TABLE assets DROP CONSTRAINT assets_kind_check;
    ALTER TABLE assets ADD CONSTRAINT assets_kind_check CHECK (kind IN ('CRYPTO','FIAT'));
    ALTER TABLE instruments DROP CONSTRAINT instruments_asset_class_check;
    ALTER TABLE instruments ADD CONSTRAINT instruments_asset_class_check
      CHECK (asset_class IN ('CRYPTO_SPOT'));
    ALTER TABLE venues DROP CONSTRAINT venues_kind_check;
    ALTER TABLE venues ADD CONSTRAINT venues_kind_check CHECK (kind IN ('EXCHANGE','SIMULATED'));
    """)
