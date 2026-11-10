"""The daily gold robot's risk per trade (S75, ADR-037 addendum).

HFM's smallest gold trade is 1 ounce. With a stop of 2 × the daily ATR (about 220 USD an ounce
in 2026), one trade of the daily robot can lose about 11 % of 2,000 at its stop, which the owner
chose on 2026-11-10. Every other mandate keeps its 5 % ceiling.

- ``trading_mandates.risk_pct`` may be up to 15 % for the rule ``gold_daily`` only (granted on
  the second HFM demo account only, checked when it is granted).

The downgrade refuses while a mandate above 5 % exists.

Revision ID: 0027_gold_daily_risk
Revises: 0026_second_hfm_demo
Create Date: 2026-11-10
"""

from cosmic.db.sqlsplit import run_script

revision = "0027_gold_daily_risk"
down_revision = "0026_second_hfm_demo"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    ALTER TABLE trading_mandates DROP CONSTRAINT trading_mandates_risk_pct_check;
    ALTER TABLE trading_mandates ADD CONSTRAINT trading_mandates_risk_pct_check CHECK (
      risk_pct IS NULL OR (
        risk_pct >= 0.1
        AND (risk_pct <= 5 OR (strategy_key = 'gold_daily' AND risk_pct <= 15))
      )
    );
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM trading_mandates WHERE risk_pct > 5) THEN
        RAISE EXCEPTION 'a mandate risks over 5 percent; the schema before 0027 cannot hold it';
      END IF;
    END $$;
    ALTER TABLE trading_mandates DROP CONSTRAINT trading_mandates_risk_pct_check;
    ALTER TABLE trading_mandates ADD CONSTRAINT trading_mandates_risk_pct_check
      CHECK (risk_pct IS NULL OR (risk_pct >= 0.1 AND risk_pct <= 5));
    """
    )
