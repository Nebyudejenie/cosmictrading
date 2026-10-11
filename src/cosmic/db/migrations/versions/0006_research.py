"""Workforce (missions, tasks, attempts, reviews, artifacts, escalations) and research
(strategies, versions, backtests, opportunities).

Revision ID: 0006_research
Revises: 0005_ai
Create Date: 2026-10-11
"""

from cosmic.db.sqlsplit import run_script

revision = "0006_research"
down_revision = "0005_ai"
branch_labels = None
depends_on = None

TASK_STATES = (
    "('QUEUED','RUNNING','WAITING_FOR_AI','BLOCKED','IN_REVIEW','DONE','FAILED','CANCELLED',"
    "'EXPIRED','ESCALATED','SKIPPED')"
)
OPP_STATES = (
    "('IDEA','RESEARCH','CHALLENGED','QUALIFIED','APPROVAL_REQUIRED','APPROVED','REJECTED','EXPIRED',"
    "'EXECUTION','REVIEW','NON_EXECUTABLE','NO_TRADE')"
)


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script(f"""
    CREATE TABLE strategies (
      id uuid PRIMARY KEY,
      key text NOT NULL UNIQUE,
      name text NOT NULL,
      description text NOT NULL,
      created_at timestamptz NOT NULL
    );
    CREATE TABLE strategy_versions (
      id uuid PRIMARY KEY,
      strategy_id uuid NOT NULL REFERENCES strategies(id),
      version integer NOT NULL,
      params jsonb NOT NULL,
      code_ref text NOT NULL,
      hash text NOT NULL UNIQUE,
      status text NOT NULL CHECK (status IN ('DRAFT','RESEARCH','QUALIFIED','RETIRED')),
      created_at timestamptz NOT NULL,
      UNIQUE (strategy_id, version)
    );
    CREATE TABLE backtest_runs (
      id uuid PRIMARY KEY,
      strategy_version_id uuid NOT NULL REFERENCES strategy_versions(id),
      instrument_id uuid NOT NULL REFERENCES instruments(id),
      kind text NOT NULL CHECK (kind IN ('BACKTEST','WALK_FORWARD')),
      timeframe text NOT NULL,
      data_from timestamptz NOT NULL,
      data_to timestamptz NOT NULL,
      bars integer NOT NULL,
      data_vintage jsonb NOT NULL,
      assumptions jsonb NOT NULL,
      trials integer NOT NULL,
      result jsonb NOT NULL,
      result_hash text NOT NULL,
      created_at timestamptz NOT NULL
    );
    CREATE INDEX backtest_runs_version ON backtest_runs (strategy_version_id, created_at);

    CREATE TABLE missions (
      id uuid PRIMARY KEY,
      kind text NOT NULL,
      trigger_event_id uuid REFERENCES market_events(id),
      instrument_id uuid REFERENCES instruments(id),
      status text NOT NULL CHECK (status IN ('OPEN','RUNNING','WAITING_FOR_AI','REVIEW','COMPLETED','FAILED','EXPIRED','ARCHIVED')),
      status_reason text,
      idempotency_key text NOT NULL UNIQUE,
      ai_calls_used integer NOT NULL DEFAULT 0,
      ai_call_budget integer NOT NULL,
      expires_at timestamptz NOT NULL,
      created_at timestamptz NOT NULL,
      updated_at timestamptz NOT NULL,
      completed_at timestamptz
    );
    CREATE INDEX missions_status ON missions (status);

    CREATE TABLE opportunities (
      id uuid PRIMARY KEY,
      mission_id uuid NOT NULL REFERENCES missions(id),
      instrument_id uuid NOT NULL REFERENCES instruments(id),
      strategy_version_id uuid REFERENCES strategy_versions(id),
      status text NOT NULL CHECK (status IN {OPP_STATES}),
      direction text,
      thesis text,
      intent_id uuid REFERENCES trade_intents(id),
      summary jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      created_at timestamptz NOT NULL,
      updated_at timestamptz NOT NULL
    );
    CREATE TABLE opportunity_transitions (
      id uuid PRIMARY KEY,
      opportunity_id uuid NOT NULL REFERENCES opportunities(id),
      from_status text,
      to_status text NOT NULL,
      reason text NOT NULL,
      occurred_at timestamptz NOT NULL
    );
    CREATE TRIGGER opportunity_transitions_immutable BEFORE UPDATE OR DELETE ON opportunity_transitions
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

    CREATE TABLE tasks (
      id uuid PRIMARY KEY,
      mission_id uuid NOT NULL REFERENCES missions(id),
      parent_task_id uuid REFERENCES tasks(id),
      role_key text NOT NULL,
      kind text NOT NULL,
      title text NOT NULL,
      status text NOT NULL CHECK (status IN {TASK_STATES}),
      priority integer NOT NULL DEFAULT 100,
      depth integer NOT NULL DEFAULT 0 CHECK (depth BETWEEN 0 AND 3),
      deadline timestamptz,
      expires_at timestamptz NOT NULL,
      input jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      output jsonb,
      acceptance jsonb NOT NULL,
      blocker text,
      idempotency_key text NOT NULL UNIQUE,
      attempts integer NOT NULL DEFAULT 0,
      max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
      ai_calls_used integer NOT NULL DEFAULT 0,
      ai_call_budget integer NOT NULL DEFAULT 0,
      reviewer_role text,
      created_at timestamptz NOT NULL,
      updated_at timestamptz NOT NULL,
      completed_at timestamptz
    );
    CREATE INDEX tasks_runnable ON tasks (status, priority);
    CREATE TABLE task_deps (
      task_id uuid NOT NULL REFERENCES tasks(id),
      depends_on uuid NOT NULL REFERENCES tasks(id),
      PRIMARY KEY (task_id, depends_on),
      CHECK (task_id <> depends_on)
    );
    CREATE TABLE task_attempts (
      id uuid PRIMARY KEY,
      task_id uuid NOT NULL REFERENCES tasks(id),
      attempt_no integer NOT NULL,
      worker_id text NOT NULL,
      started_at timestamptz NOT NULL,
      finished_at timestamptz,
      status text NOT NULL,
      error text
    );
    CREATE TABLE task_reviews (
      id uuid PRIMARY KEY,
      task_id uuid NOT NULL REFERENCES tasks(id),
      reviewer_role text NOT NULL,
      verdict text NOT NULL CHECK (verdict IN ('PASS','FAIL')),
      checks jsonb NOT NULL,
      created_at timestamptz NOT NULL
    );
    CREATE TABLE artifacts (
      id uuid PRIMARY KEY,
      mission_id uuid NOT NULL REFERENCES missions(id),
      task_id uuid REFERENCES tasks(id),
      kind text NOT NULL,
      content jsonb NOT NULL,
      content_hash text NOT NULL,
      created_at timestamptz NOT NULL
    );
    CREATE INDEX artifacts_mission ON artifacts (mission_id);
    CREATE TRIGGER artifacts_immutable BEFORE UPDATE OR DELETE ON artifacts
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    CREATE TABLE escalations (
      id uuid PRIMARY KEY,
      task_id uuid NOT NULL REFERENCES tasks(id),
      reason text NOT NULL,
      created_at timestamptz NOT NULL,
      resolved_at timestamptz
    );

    ALTER TABLE claims ADD CONSTRAINT claims_mission_fk FOREIGN KEY (mission_id) REFERENCES missions(id);
    ALTER TABLE trade_intents ADD CONSTRAINT trade_intents_opportunity_fk
      FOREIGN KEY (opportunity_id) REFERENCES opportunities(id);
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    ALTER TABLE trade_intents DROP CONSTRAINT trade_intents_opportunity_fk;
    ALTER TABLE claims DROP CONSTRAINT claims_mission_fk;
    DROP TABLE escalations;
    DROP TABLE artifacts;
    DROP TABLE task_reviews;
    DROP TABLE task_attempts;
    DROP TABLE task_deps;
    DROP TABLE tasks;
    DROP TABLE opportunity_transitions;
    DROP TABLE opportunities;
    DROP TABLE missions;
    DROP TABLE backtest_runs;
    DROP TABLE strategy_versions;
    DROP TABLE strategies;
    """)
