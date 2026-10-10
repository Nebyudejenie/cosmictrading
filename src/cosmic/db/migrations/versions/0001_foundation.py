"""Foundation: identity, sessions, hash-chained audit, jobs, incidents, notifications.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-10-10
"""

from cosmic.db.sqlsplit import run_script

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None

AUDIT_LOCK_KEY = 7201001


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(f"""
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app};
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {app};
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO {app};
    REVOKE ALL ON alembic_version FROM {app};
    GRANT SELECT ON alembic_version TO {app};

    CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      RAISE EXCEPTION 'table % is append-only: % rejected', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
    END $$;

    CREATE TABLE users (
      id uuid PRIMARY KEY,
      username text NOT NULL UNIQUE
        CHECK (username = lower(username) AND length(username) BETWEEN 3 AND 64),
      display_name text NOT NULL,
      password_hash text NOT NULL,
      roles text[] NOT NULL CHECK (cardinality(roles) >= 1),
      is_active boolean NOT NULL DEFAULT true,
      mfa_secret_enc bytea,
      mfa_enabled boolean NOT NULL DEFAULT false,
      mfa_last_step bigint,
      telegram_user_id bigint UNIQUE,
      failed_logins integer NOT NULL DEFAULT 0,
      locked_until timestamptz,
      password_changed_at timestamptz NOT NULL,
      created_at timestamptz NOT NULL,
      updated_at timestamptz NOT NULL,
      CHECK (NOT mfa_enabled OR mfa_secret_enc IS NOT NULL)
    );

    CREATE TABLE sessions (
      id uuid PRIMARY KEY,
      user_id uuid NOT NULL REFERENCES users(id),
      token_hash bytea NOT NULL UNIQUE,
      csrf_token text NOT NULL,
      created_at timestamptz NOT NULL,
      last_seen_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      mfa_verified_at timestamptz,
      revoked_at timestamptz,
      revoked_reason text,
      ip_address text,
      user_agent text
    );
    CREATE INDEX sessions_user_active ON sessions (user_id) WHERE revoked_at IS NULL;

    CREATE TABLE audit_events (
      seq bigint PRIMARY KEY,
      id uuid NOT NULL UNIQUE,
      occurred_at timestamptz NOT NULL,
      actor_type text NOT NULL CHECK (actor_type IN ('USER','SYSTEM','WORKER','TELEGRAM','CLI')),
      actor_id text,
      action text NOT NULL,
      entity_type text,
      entity_id text,
      correlation_id text,
      payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      prev_hash text NOT NULL,
      hash text NOT NULL UNIQUE
    );
    CREATE INDEX audit_events_entity ON audit_events (entity_type, entity_id);
    CREATE INDEX audit_events_occurred ON audit_events (occurred_at);

    CREATE FUNCTION audit_event_hash(
      p_prev text, p_seq bigint, p_occurred timestamptz, p_actor_type text, p_actor_id text,
      p_action text, p_entity_type text, p_entity_id text, p_correlation_id text, p_payload jsonb
    ) RETURNS text LANGUAGE sql STABLE AS $$
      SELECT encode(sha256(convert_to(
        p_prev || '|' || p_seq::text || '|' ||
        to_char(p_occurred AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') || '|' ||
        p_actor_type || '|' || coalesce(p_actor_id, '') || '|' || p_action || '|' ||
        coalesce(p_entity_type, '') || '|' || coalesce(p_entity_id, '') || '|' ||
        coalesce(p_correlation_id, '') || '|' || p_payload::text,
        'UTF8')), 'hex')
    $$;

    CREATE FUNCTION audit_events_chain() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      last_seq bigint;
      last_hash text;
    BEGIN
      PERFORM pg_advisory_xact_lock({AUDIT_LOCK_KEY});
      SELECT seq, hash INTO last_seq, last_hash FROM audit_events ORDER BY seq DESC LIMIT 1;
      IF last_seq IS NULL THEN
        last_seq := 0;
        last_hash := repeat('0', 64);
      END IF;
      NEW.seq := last_seq + 1;
      NEW.prev_hash := last_hash;
      NEW.hash := audit_event_hash(NEW.prev_hash, NEW.seq, NEW.occurred_at, NEW.actor_type,
        NEW.actor_id, NEW.action, NEW.entity_type, NEW.entity_id, NEW.correlation_id, NEW.payload);
      RETURN NEW;
    END $$;

    CREATE TRIGGER audit_events_chain BEFORE INSERT ON audit_events
      FOR EACH ROW EXECUTE FUNCTION audit_events_chain();
    CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events
      FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

    CREATE FUNCTION audit_verify() RETURNS TABLE (ok boolean, checked bigint, first_bad_seq bigint, reason text)
    LANGUAGE plpgsql STABLE AS $$
    DECLARE
      r record;
      expected_seq bigint := 1;
      expected_prev text := repeat('0', 64);
      n bigint := 0;
    BEGIN
      FOR r IN SELECT * FROM audit_events ORDER BY seq LOOP
        n := n + 1;
        IF r.seq <> expected_seq THEN
          RETURN QUERY SELECT false, n, expected_seq, 'sequence gap'::text; RETURN;
        END IF;
        IF r.prev_hash <> expected_prev THEN
          RETURN QUERY SELECT false, n, r.seq, 'previous hash mismatch'::text; RETURN;
        END IF;
        IF r.hash <> audit_event_hash(r.prev_hash, r.seq, r.occurred_at, r.actor_type, r.actor_id,
            r.action, r.entity_type, r.entity_id, r.correlation_id, r.payload) THEN
          RETURN QUERY SELECT false, n, r.seq, 'content hash mismatch'::text; RETURN;
        END IF;
        expected_seq := r.seq + 1;
        expected_prev := r.hash;
      END LOOP;
      RETURN QUERY SELECT true, n, NULL::bigint, NULL::text;
    END $$;

    REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM {app};

    CREATE TABLE jobs (
      id uuid PRIMARY KEY,
      kind text NOT NULL,
      payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      priority smallint NOT NULL DEFAULT 100,
      run_at timestamptz NOT NULL,
      status text NOT NULL CHECK (status IN ('QUEUED','RUNNING','DONE','FAILED','DEAD','CANCELLED')),
      attempts integer NOT NULL DEFAULT 0,
      max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
      locked_by text,
      locked_until timestamptz,
      idempotency_key text UNIQUE,
      last_error text,
      created_at timestamptz NOT NULL,
      started_at timestamptz,
      finished_at timestamptz
    );
    CREATE INDEX jobs_ready ON jobs (priority, run_at) WHERE status = 'QUEUED';
    CREATE INDEX jobs_running ON jobs (locked_until) WHERE status = 'RUNNING';

    CREATE TABLE worker_heartbeats (
      worker_id text PRIMARY KEY,
      process text NOT NULL,
      started_at timestamptz NOT NULL,
      last_beat_at timestamptz NOT NULL,
      info jsonb NOT NULL DEFAULT '{{}}'::jsonb
    );

    CREATE TABLE incidents (
      id uuid PRIMARY KEY,
      dedup_key text NOT NULL,
      category text NOT NULL CHECK (category IN ('ACCOUNTING','SECURITY','DATA','AI','OPS','TRADING')),
      severity text NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
      status text NOT NULL CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
      title text NOT NULL,
      detail jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      occurrences integer NOT NULL DEFAULT 1,
      opened_at timestamptz NOT NULL,
      last_seen_at timestamptz NOT NULL,
      acknowledged_at timestamptz,
      acknowledged_by uuid REFERENCES users(id),
      resolved_at timestamptz,
      resolved_by uuid REFERENCES users(id),
      resolution text
    );
    CREATE UNIQUE INDEX incidents_open_dedup ON incidents (dedup_key) WHERE status <> 'RESOLVED';

    CREATE TABLE notifications (
      id uuid PRIMARY KEY,
      user_id uuid REFERENCES users(id),
      channel text NOT NULL CHECK (channel IN ('TELEGRAM','PORTAL')),
      kind text NOT NULL,
      payload jsonb NOT NULL,
      status text NOT NULL CHECK (status IN ('PENDING','SENT','FAILED','SKIPPED')),
      attempts integer NOT NULL DEFAULT 0,
      dedup_key text UNIQUE,
      last_error text,
      created_at timestamptz NOT NULL,
      sent_at timestamptz
    );
    CREATE INDEX notifications_pending ON notifications (created_at) WHERE status = 'PENDING';
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(f"""
    DROP TABLE notifications;
    DROP TABLE incidents;
    DROP TABLE worker_heartbeats;
    DROP TABLE jobs;
    DROP TABLE audit_events;
    DROP FUNCTION audit_verify();
    DROP FUNCTION audit_events_chain();
    DROP FUNCTION audit_event_hash(text, bigint, timestamptz, text, text, text, text, text, text, jsonb);
    DROP TABLE sessions;
    DROP TABLE users;
    DROP FUNCTION forbid_mutation();
    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {app};
    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {app};
    ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM {app};
    """)
