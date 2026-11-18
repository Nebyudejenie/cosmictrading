"""Each simulated gold account trades one rule, for good (S102).

The owner (2026-11-18, the master specification): two independent robots, never merged. Robot A,
the TEST robot (the lab's best candidate, ``cosmic.gold.trend_buy``), runs in the simulation at
100 USD, because no HFM demo the owner can open trades less than 1 ounce of gold and its stop
(about 13 USD an ounce) would then risk far more than their 2 USD. The simulation held one
account at a time with one rule; now:

- ``gold_sim_accounts.rule``: the rule an account trades, set when it starts and never changed
  (the existing guard lets only its status change): ``gold_sim`` (the gold reading, every
  account so far) or ``gold_trend_buy`` (Robot A).
- ``gold_sim_decisions.evaluation``: what was weighed before a trade (the owner's section 5: the
  probability of winning and of losing, the expected reward, loss and value, the reward to risk,
  the confidence, the reasons for and against, what the connected HFM demo would have needed).
  The table stays append-only.

The downgrade refuses while an account trades another rule than ``gold_sim``: its rule, and the
evaluations of its decisions, would be lost.

Revision ID: 0035_sim_rule
Revises: 0034_hot_lookups
Create Date: 2026-11-18
"""

from cosmic.db.sqlsplit import run_script

revision = "0035_sim_rule"
down_revision = "0034_hot_lookups"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    ALTER TABLE gold_sim_accounts ADD COLUMN rule text NOT NULL DEFAULT 'gold_sim'
      CONSTRAINT gold_sim_accounts_rule_check CHECK (rule IN ('gold_sim', 'gold_trend_buy'));
    ALTER TABLE gold_sim_decisions ADD COLUMN evaluation jsonb;
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM gold_sim_accounts WHERE rule <> 'gold_sim') THEN
        RAISE EXCEPTION 'a simulated account trades the TEST rule; the schema before 0035 cannot hold it';
      END IF;
    END $$;
    ALTER TABLE gold_sim_decisions DROP COLUMN evaluation;
    ALTER TABLE gold_sim_accounts DROP COLUMN rule;
    """)
