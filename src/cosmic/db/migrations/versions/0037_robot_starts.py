"""START when ready (S111, the owner's request of 2026-11-21 to make the manual steps automatic).

- ``robot_starts``: the owner's approval, given in advance with their authenticator code, to
  start Robot A the moment its own HFM demo is ready (its terminal's check passed and its account
  opened by itself, ``gold.robot_setup``). The terms it starts with are kept with it. One waiting
  at a time; it ends STARTED (with the mandate it granted), REFUSED (a check of the grant
  refused it, with why), CANCELLED (by the owner) or EXPIRED, and only its ending is ever
  written, once.

The downgrade refuses while any approval in advance exists.

Revision ID: 0037_robot_starts
Revises: 0036_trend_demo_risk
Create Date: 2026-11-21
"""

from cosmic.db.sqlsplit import run_script

revision = "0037_robot_starts"
down_revision = "0036_trend_demo_risk"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        f"""
    CREATE TABLE robot_starts (
      id uuid PRIMARY KEY,
      robot text NOT NULL CHECK (robot IN ('ROBOT_A')),
      owner_id uuid NOT NULL REFERENCES users (id),
      armed_at timestamptz NOT NULL,
      armed_by text NOT NULL,
      expires_at timestamptz NOT NULL,
      terms jsonb NOT NULL,
      ended text CHECK (ended IN ('STARTED','REFUSED','CANCELLED','EXPIRED')),
      ended_at timestamptz,
      mandate_id uuid REFERENCES trading_mandates (id),
      reason text,
      CHECK (expires_at > armed_at),
      CHECK ((ended IS NULL) = (ended_at IS NULL)),
      CHECK ((ended = 'STARTED') = (mandate_id IS NOT NULL))
    );
    CREATE UNIQUE INDEX robot_starts_one_waiting ON robot_starts (robot) WHERE ended IS NULL;
    CREATE FUNCTION robot_starts_sealed() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.ended IS NOT NULL OR NEW.ended IS NULL
         OR NEW.id <> OLD.id OR NEW.robot <> OLD.robot OR NEW.owner_id <> OLD.owner_id
         OR NEW.armed_at <> OLD.armed_at OR NEW.armed_by <> OLD.armed_by
         OR NEW.expires_at <> OLD.expires_at OR NEW.terms <> OLD.terms THEN
        RAISE EXCEPTION 'an approval in advance is sealed: only its ending is recorded, once'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER robot_starts_sealed BEFORE UPDATE ON robot_starts
      FOR EACH ROW EXECUTE FUNCTION robot_starts_sealed();
    REVOKE DELETE, TRUNCATE ON robot_starts FROM {app};
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM robot_starts) THEN
        RAISE EXCEPTION 'approvals in advance exist; the schema before 0037 cannot hold them';
      END IF;
    END $$;
    DROP TABLE robot_starts;
    DROP FUNCTION robot_starts_sealed();
    """
    )
