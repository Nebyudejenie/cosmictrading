"""Strategy library: evaluation runs, per-rule results and owner activations (QNT-13).

All three tables are append-only: an evaluation is evidence and is never rewritten, and the
switch that lets a rule propose paper trades is a history of owner decisions (latest wins).

Revision ID: 0008_strategy_library
Revises: 0007_search
Create Date: 2026-10-14
"""

from cosmic.db.sqlsplit import run_script

revision = "0008_strategy_library"
down_revision = "0007_search"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(f"""
    CREATE TABLE library_runs (
      id uuid PRIMARY KEY,
      started_at timestamptz NOT NULL,
      finished_at timestamptz NOT NULL,
      code_version text NOT NULL,
      trials integer NOT NULL CHECK (trials >= 0),
      costs jsonb NOT NULL,
      series jsonb NOT NULL,
      summary jsonb NOT NULL,
      result_hash text NOT NULL,
      created_at timestamptz NOT NULL
    );
    CREATE TRIGGER library_runs_immutable BEFORE UPDATE OR DELETE ON library_runs
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON library_runs FROM {app};

    CREATE TABLE strategy_evaluations (
      id uuid PRIMARY KEY,
      library_run_id uuid NOT NULL REFERENCES library_runs(id),
      strategy_version_id uuid NOT NULL REFERENCES strategy_versions(id),
      series text NOT NULL,
      instrument_id uuid REFERENCES instruments(id),
      tier text NOT NULL CHECK (tier IN ('QUALIFIED','WATCH','REJECTED')),
      rank integer NOT NULL CHECK (rank >= 1),
      deflated_sharpe numeric(18,8) NOT NULL,
      timing_sharpe numeric(18,8) NOT NULL,
      result jsonb NOT NULL,
      created_at timestamptz NOT NULL,
      UNIQUE (library_run_id, strategy_version_id, series)
    );
    CREATE INDEX strategy_evaluations_run ON strategy_evaluations (library_run_id, series, rank);
    CREATE TRIGGER strategy_evaluations_immutable BEFORE UPDATE OR DELETE ON strategy_evaluations
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON strategy_evaluations FROM {app};

    CREATE TABLE strategy_activations (
      id uuid PRIMARY KEY,
      strategy_key text NOT NULL,
      instrument_id uuid NOT NULL REFERENCES instruments(id),
      action text NOT NULL CHECK (action IN ('ACTIVATE','DEACTIVATE')),
      evaluation_id uuid REFERENCES strategy_evaluations(id),
      actor_type text NOT NULL,
      actor_id text NOT NULL,
      reason text NOT NULL,
      occurred_at timestamptz NOT NULL,
      CHECK (action = 'DEACTIVATE' OR evaluation_id IS NOT NULL)
    );
    CREATE INDEX strategy_activations_latest
      ON strategy_activations (strategy_key, instrument_id, occurred_at);
    CREATE TRIGGER strategy_activations_immutable BEFORE UPDATE OR DELETE ON strategy_activations
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON strategy_activations FROM {app};
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DROP TABLE strategy_activations;
    DROP TABLE strategy_evaluations;
    DROP TABLE library_runs;
    """)
