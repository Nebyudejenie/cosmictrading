"""Robot controls (S79): the owner's rules and profit goals for a gold robot, and its status.

The P0 demo directive (2026-11-11) asks for configurable risk rules, a profit goal with a safe
shutdown, the same at the daily loss limit, and PAUSE / RESUME. A mandate's terms are immutable
(0011), so these live beside it, one row per mandate:

- ``robot_controls``: the pause after N losing trades in a row, the take-profit multiple of the
  whole risk, the daily / session / long-term profit targets and how they are measured (net
  realized, or net realized plus floating), and the status: RUNNING, PAUSED, TARGET_REACHED,
  LOSS_LIMIT. Every change is also written to the hash-chained audit.

The downgrade refuses while a control exists.

Revision ID: 0028_robot_controls
Revises: 0027_gold_daily_risk
Create Date: 2026-11-11
"""

from cosmic.db.sqlsplit import run_script

revision = "0028_robot_controls"
down_revision = "0027_gold_daily_risk"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        f"""
    CREATE TABLE robot_controls (
      mandate_id uuid PRIMARY KEY REFERENCES trading_mandates(id),
      pause_after_losses integer NOT NULL CHECK (pause_after_losses BETWEEN 1 AND 20),
      take_profit_r numeric(38,18) NOT NULL CHECK (take_profit_r >= 0.5 AND take_profit_r <= 10),
      daily_target numeric(38,18) CHECK (daily_target IS NULL OR daily_target > 0),
      session_target numeric(38,18) CHECK (session_target IS NULL OR session_target > 0),
      long_target numeric(38,18) CHECK (long_target IS NULL OR long_target > 0),
      measure text NOT NULL CHECK (measure IN ('REALIZED','REALIZED_AND_FLOATING')),
      session_started_at timestamptz NOT NULL,
      status text NOT NULL CHECK (status IN ('RUNNING','PAUSED','TARGET_REACHED','LOSS_LIMIT')),
      status_reason text NOT NULL,
      status_at timestamptz NOT NULL,
      final_net numeric(38,18),
      updated_at timestamptz NOT NULL,
      updated_by text NOT NULL
    );
    REVOKE DELETE, TRUNCATE ON robot_controls FROM {app};
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM robot_controls) THEN
        RAISE EXCEPTION 'robot controls exist; the schema before 0028 cannot hold them';
      END IF;
    END $$;
    DROP TABLE robot_controls;
    """
    )
