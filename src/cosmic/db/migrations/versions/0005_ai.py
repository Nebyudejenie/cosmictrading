"""Free-only AI gateway: attestations, model approvals, provider state, usage, cache.

Revision ID: 0005_ai
Revises: 0004_execution
Create Date: 2026-10-11
"""

from cosmic.db.sqlsplit import run_script

revision = "0005_ai"
down_revision = "0004_execution"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(f"""
    CREATE TABLE ai_provider_attestations (
      id uuid PRIMARY KEY,
      provider_key text NOT NULL,
      statement text NOT NULL,
      evidence_url text NOT NULL,
      rpm_limit integer CHECK (rpm_limit IS NULL OR rpm_limit >= 0),
      rpd_limit integer CHECK (rpd_limit IS NULL OR rpd_limit >= 0),
      attested_by uuid NOT NULL REFERENCES users(id),
      attested_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      revoked_at timestamptz,
      CHECK (expires_at > attested_at)
    );
    CREATE INDEX ai_attestations_provider ON ai_provider_attestations (provider_key, attested_at);

    CREATE TABLE ai_model_approvals (
      id uuid PRIMARY KEY,
      provider_key text NOT NULL,
      model_id text NOT NULL,
      tier text NOT NULL CHECK (tier IN ('SMALL','LARGE')),
      context_limit integer,
      evidence_url text NOT NULL,
      approved_by uuid NOT NULL REFERENCES users(id),
      approved_at timestamptz NOT NULL,
      review_expires_at timestamptz NOT NULL,
      revoked_at timestamptz,
      price_checked_at timestamptz,
      price_check_result text
    );
    CREATE UNIQUE INDEX ai_model_approvals_active ON ai_model_approvals (provider_key, model_id) WHERE revoked_at IS NULL;

    CREATE TABLE ai_provider_state (
      provider_key text PRIMARY KEY,
      circuit text NOT NULL CHECK (circuit IN ('CLOSED','OPEN','HALF_OPEN')),
      consecutive_failures integer NOT NULL DEFAULT 0,
      opened_until timestamptz,
      rate_limited_until timestamptz,
      last_success_at timestamptz,
      last_error_at timestamptz,
      last_error text,
      disabled_reason text,
      updated_at timestamptz NOT NULL
    );

    CREATE TABLE ai_usage (
      id uuid PRIMARY KEY,
      provider_key text NOT NULL,
      model_id text NOT NULL,
      task_type text NOT NULL,
      role_key text,
      request_hash text NOT NULL,
      status text NOT NULL CHECK (status IN ('OK','RATE_LIMITED','ERROR','INVALID_OUTPUT','BILLING_SIGNAL','TIMEOUT')),
      http_status integer,
      prompt_tokens integer,
      completion_tokens integer,
      latency_ms integer,
      reported_cost numeric(38,18),
      error text,
      created_at timestamptz NOT NULL
    );
    CREATE INDEX ai_usage_provider_time ON ai_usage (provider_key, created_at);
    CREATE TRIGGER ai_usage_immutable BEFORE UPDATE OR DELETE ON ai_usage
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON ai_usage FROM {app};

    CREATE TABLE ai_cache (
      cache_key text PRIMARY KEY,
      task_type text NOT NULL,
      response jsonb NOT NULL,
      provider_key text NOT NULL,
      model_id text NOT NULL,
      created_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL
    );
    CREATE INDEX ai_cache_expiry ON ai_cache (expires_at);
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DROP TABLE ai_cache;
    DROP TABLE ai_usage;
    DROP TABLE ai_provider_state;
    DROP TABLE ai_model_approvals;
    DROP TABLE ai_provider_attestations;
    """)
