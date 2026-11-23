"""The owner's real account may be at Exness, read-only (S115, the owner's choice of 2026-11-23).

``broker_connections.broker`` was HFM only. It now allows EXNESS too, and only for the owner's
real account as Manual Trading reads it: declared REAL, with its investor (read-only) password,
never with an execution switch. The robots' demo accounts stay HFM. The downgrade refuses while
an Exness account is saved.

Revision ID: 0039_exness_read_only
Revises: 0038_manual_trading
Create Date: 2026-11-23
"""

from cosmic.db.sqlsplit import run_script

revision = "0039_exness_read_only"
down_revision = "0038_manual_trading"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    ALTER TABLE broker_connections DROP CONSTRAINT broker_connections_broker_check;
    ALTER TABLE broker_connections ADD CONSTRAINT broker_connections_broker_check
      CHECK (broker IN ('HFM','EXNESS'));
    -- Exness: only the owner's real account, read-only (Manual Trading); never a trading switch.
    ALTER TABLE broker_connections ADD CONSTRAINT broker_connections_exness_read_only
      CHECK (broker = 'HFM' OR (declared_type = 'REAL' AND password_kind = 'INVESTOR'
                                AND NOT execution_enabled));
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM broker_connections WHERE broker <> 'HFM') THEN
        RAISE EXCEPTION 'an Exness account is saved: it is kept, so 0039 is not undone';
      END IF;
    END $$;
    ALTER TABLE broker_connections DROP CONSTRAINT broker_connections_exness_read_only;
    ALTER TABLE broker_connections DROP CONSTRAINT broker_connections_broker_check;
    ALTER TABLE broker_connections ADD CONSTRAINT broker_connections_broker_check
      CHECK (broker = 'HFM');
    """
    )
