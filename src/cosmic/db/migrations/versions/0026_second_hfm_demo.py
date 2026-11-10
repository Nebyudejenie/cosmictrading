"""A second HFM demo account with its own bridge (S74, ADR-028 addendum).

The owner chose on 2026-11-10 to run a daily gold robot next to the hourly one, on a second HFM
MT5 demo account with its own MetaTrader 5 terminal and bridge. Positions are one row per
account and instrument, so two robots on one account would net a buy against a short; each robot
therefore has its own account.

- ``accounts.broker_adapter`` of a SANDBOX account may be ``hfm_mt5_demo_2``.

The downgrade refuses while an account on that adapter exists.

Revision ID: 0026_second_hfm_demo
Revises: 0025_selftest_replaced
Create Date: 2026-11-10
"""

from cosmic.db.sqlsplit import run_script

revision = "0026_second_hfm_demo"
down_revision = "0025_selftest_replaced"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    ALTER TABLE accounts DROP CONSTRAINT accounts_adapter_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_adapter_check CHECK (
      (kind = 'PAPER' AND broker_adapter = 'paper')
      OR (kind = 'SANDBOX'
          AND broker_adapter IN ('binance_spot_testnet','hfm_mt5_demo','hfm_mt5_demo_2'))
      OR (kind = 'LIVE' AND broker_adapter = 'binance_spot_live')
    );
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM accounts WHERE broker_adapter = 'hfm_mt5_demo_2') THEN
        RAISE EXCEPTION 'the second HFM demo has an account; the schema before 0026 cannot hold it';
      END IF;
    END $$;
    ALTER TABLE accounts DROP CONSTRAINT accounts_adapter_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_adapter_check CHECK (
      (kind = 'PAPER' AND broker_adapter = 'paper')
      OR (kind = 'SANDBOX' AND broker_adapter IN ('binance_spot_testnet','hfm_mt5_demo'))
      OR (kind = 'LIVE' AND broker_adapter = 'binance_spot_live')
    );
    """
    )
