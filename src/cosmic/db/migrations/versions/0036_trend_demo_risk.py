"""Robot A's own HFM demo: its risk per trade (S110, the owner's choice of 2026-11-20).

The owner asked that Robot A trade real orders on an HFM demo ("real way, not simulation") and
chose, with the numbers before them, HFM's smallest gold trade (1 ounce) at Robot A's stop: about
10 to 25 USD of the 100 USD demo a trade, at most 30. Every other mandate keeps its ceiling (5 %,
or 15 % for the daily gold robot).

- ``trading_mandates.risk_pct`` may be up to 30 % for the rule ``gold_trend_buy`` only. A mandate
  lives on a simulated or demo account only (the database refuses any other, 0011), and this rule
  is granted only on the second HFM demo account (checked when it is granted).

The downgrade refuses while a mandate above 15 % exists.

Revision ID: 0036_trend_demo_risk
Revises: 0035_sim_rule
Create Date: 2026-11-20
"""

from cosmic.db.sqlsplit import run_script

revision = "0036_trend_demo_risk"
down_revision = "0035_sim_rule"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    ALTER TABLE trading_mandates DROP CONSTRAINT trading_mandates_risk_pct_check;
    ALTER TABLE trading_mandates ADD CONSTRAINT trading_mandates_risk_pct_check CHECK (
      risk_pct IS NULL OR (
        risk_pct >= 0.1
        AND (
          risk_pct <= 5
          OR (strategy_key = 'gold_daily' AND risk_pct <= 15)
          OR (strategy_key = 'gold_trend_buy' AND risk_pct <= 30)
        )
      )
    );
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM trading_mandates
        WHERE risk_pct > 15 OR (risk_pct > 5 AND strategy_key <> 'gold_daily')
      ) THEN
        RAISE EXCEPTION 'a mandate risks more than 0035 allows; the schema before 0036 cannot hold it';
      END IF;
    END $$;
    ALTER TABLE trading_mandates DROP CONSTRAINT trading_mandates_risk_pct_check;
    ALTER TABLE trading_mandates ADD CONSTRAINT trading_mandates_risk_pct_check CHECK (
      risk_pct IS NULL OR (
        risk_pct >= 0.1
        AND (risk_pct <= 5 OR (strategy_key = 'gold_daily' AND risk_pct <= 15))
      )
    );
    """
    )
