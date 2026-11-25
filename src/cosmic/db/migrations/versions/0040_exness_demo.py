"""The owner's Exness demo, to test the portal's orders (S119, the owner's choice of 2026-11-25,
ADR-042).

Two checks widen, and only for a DEMO account:

- ``accounts_adapter_check``: a SANDBOX account may use ``exness_mt5_demo``, the adapter of the
  second bridge on terminal 3, which works only while its own Exness demo is signed in there.
- ``broker_connections_exness_read_only``: an Exness connection was the owner's real account
  only, read-only. It may now also be a DEMO, with that demo's own (master) password, which may
  trade once it is verified a demo (the existing checks still hold: a REAL account never has an
  execution switch, and only a CONNECTED, verified DEMO may have one).

The downgrade refuses while an Exness demo account or connection is saved.

Revision ID: 0040_exness_demo
Revises: 0039_exness_read_only
Create Date: 2026-11-25
"""

from cosmic.db.sqlsplit import run_script

revision = "0040_exness_demo"
down_revision = "0039_exness_read_only"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    ALTER TABLE accounts DROP CONSTRAINT accounts_adapter_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_adapter_check CHECK (
      (kind = 'PAPER' AND broker_adapter = 'paper')
      OR (kind = 'SANDBOX'
          AND broker_adapter IN ('binance_spot_testnet','hfm_mt5_demo','hfm_mt5_demo_2',
                                 'exness_mt5_demo'))
      OR (kind = 'LIVE' AND broker_adapter = 'binance_spot_live')
    );
    -- Exness: the owner's real account, read-only; or their Exness demo (S119).
    ALTER TABLE broker_connections DROP CONSTRAINT broker_connections_exness_read_only;
    ALTER TABLE broker_connections ADD CONSTRAINT broker_connections_exness_read_only
      CHECK (broker = 'HFM'
             OR (declared_type = 'REAL' AND password_kind = 'INVESTOR' AND NOT execution_enabled)
             OR (declared_type = 'DEMO' AND password_kind = 'MASTER'));
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM accounts WHERE broker_adapter = 'exness_mt5_demo') THEN
        RAISE EXCEPTION 'the Exness demo has an account; the schema before 0040 cannot hold it';
      END IF;
      IF EXISTS (SELECT 1 FROM broker_connections
                 WHERE broker = 'EXNESS' AND declared_type = 'DEMO') THEN
        RAISE EXCEPTION 'an Exness demo connection is saved: it is kept, so 0040 is not undone';
      END IF;
    END $$;
    ALTER TABLE broker_connections DROP CONSTRAINT broker_connections_exness_read_only;
    ALTER TABLE broker_connections ADD CONSTRAINT broker_connections_exness_read_only
      CHECK (broker = 'HFM' OR (declared_type = 'REAL' AND password_kind = 'INVESTOR'
                                AND NOT execution_enabled));
    ALTER TABLE accounts DROP CONSTRAINT accounts_adapter_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_adapter_check CHECK (
      (kind = 'PAPER' AND broker_adapter = 'paper')
      OR (kind = 'SANDBOX'
          AND broker_adapter IN ('binance_spot_testnet','hfm_mt5_demo','hfm_mt5_demo_2'))
      OR (kind = 'LIVE' AND broker_adapter = 'binance_spot_live')
    );
    """
    )
