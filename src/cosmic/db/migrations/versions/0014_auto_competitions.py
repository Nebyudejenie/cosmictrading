"""The rule competition (ADR-031): its runs, append-only.

- ``auto_competitions``: one row per run of the competition, with the code version, the
  pre-registered start of the forward test, the last bar used, the number of trials and the
  whole leaderboard (``result``) with its hash. Rows are never changed or deleted (trigger and
  grants), so every verdict can be traced to the run that made it.

The downgrade refuses while runs exist (nothing is deleted).

Revision ID: 0014_auto_competitions
Revises: 0013_mandate_total_stop
Create Date: 2026-10-22
"""

from cosmic.db.sqlsplit import run_script

revision = "0014_auto_competitions"
down_revision = "0013_mandate_total_stop"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(f"""
    CREATE TABLE auto_competitions (
      id uuid PRIMARY KEY,
      created_at timestamptz NOT NULL,
      code_version text NOT NULL,
      forward_from timestamptz NOT NULL,
      data_until timestamptz NOT NULL,
      trials integer NOT NULL CHECK (trials >= 0),
      result jsonb NOT NULL,
      result_hash text NOT NULL
    );
    CREATE INDEX auto_competitions_latest ON auto_competitions (created_at);
    CREATE TRIGGER auto_competitions_immutable BEFORE UPDATE OR DELETE ON auto_competitions
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON auto_competitions FROM {app};
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM auto_competitions) THEN
        RAISE EXCEPTION 'competition runs exist; the schema before 0014 cannot hold them';
      END IF;
    END $$;
    DROP TABLE auto_competitions;
    """)
