"""HFM MT5 account connections managed in the portal (S83, the owner's directive of 2026-11-11:
"Admin → Broker Connections → HFM MT5"; ADR-028 addendum).

The owner adds, tests, saves, edits, disconnects and reconnects MT5 accounts in the portal, and
assigns the robots to them, without touching the server. Each account is bound to one MT5
terminal on the bridge computer (its "slot").

- ``broker_connections``: one row per MT5 account (login and server). What the owner declared
  (DEMO or REAL) is kept apart from what HFM itself reported (``verified_type``): only the latter
  counts. A REAL account is held only with its read-only (investor) password and its execution
  switch can never be turned on from here (both enforced below), so connecting it can never
  trade. Never deleted; disconnected accounts stay with their history.
- ``broker_credentials``: the password, only encrypted (AES-GCM, a key derived from the server's
  own secret, the account bound in as associated data); one active per account; replaced or
  revoked, never deleted, the sealed value itself never changed.
- ``broker_connection_events``: append-only history: tests, sign-ins, health, assignments,
  refusals, each with HFM's or the bridge's own answer.
- ``robot_assignments``: which account each robot uses (the Demo Robot, the Live Robot).

The downgrade refuses while any of these tables holds a row.

Revision ID: 0030_broker_connections
Revises: 0029_ai_provider_keys
Create Date: 2026-11-11
"""

from cosmic.db.sqlsplit import run_script

revision = "0030_broker_connections"
down_revision = "0029_ai_provider_keys"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        f"""
    CREATE TABLE broker_connections (
      id uuid PRIMARY KEY,
      nickname text NOT NULL CHECK (length(nickname) BETWEEN 1 AND 60),
      broker text NOT NULL CHECK (broker = 'HFM'),
      platform text NOT NULL CHECK (platform = 'MT5'),
      login bigint NOT NULL CHECK (login > 0),
      server text NOT NULL CHECK (server ~ '^[A-Za-z0-9][A-Za-z0-9 ._-]{{1,63}}$'),
      fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{{16}}$'),
      declared_type text NOT NULL CHECK (declared_type IN ('DEMO','REAL')),
      password_kind text NOT NULL CHECK (password_kind IN ('MASTER','INVESTOR')),
      slot text CHECK (slot IS NULL OR slot ~ '^[a-z0-9][a-z0-9-]{{1,40}}$'),
      status text NOT NULL CHECK (status IN ('NEW','CONNECTED','FAILED','DISCONNECTED')),
      verified_type text CHECK (verified_type IN ('DEMO','REAL','CONTEST')),
      verified_at timestamptz,
      execution_enabled boolean NOT NULL DEFAULT false,
      health text NOT NULL DEFAULT 'UNKNOWN'
        CHECK (health IN ('UNKNOWN','OK','DEGRADED','DOWN')),
      health_reason text,
      last_sync_at timestamptz,
      login_failures integer NOT NULL DEFAULT 0 CHECK (login_failures >= 0),
      last_login_at timestamptz,
      last_error text,
      created_at timestamptz NOT NULL,
      created_by text NOT NULL,
      updated_at timestamptz NOT NULL,
      updated_by text NOT NULL,
      -- REAL: the read-only password only, and never an execution switch from here.
      CHECK (declared_type = 'DEMO' OR password_kind = 'INVESTOR'),
      CHECK (declared_type = 'DEMO' OR NOT execution_enabled),
      CHECK (NOT execution_enabled OR (status = 'CONNECTED' AND verified_type = 'DEMO')),
      CHECK (verified_type IS NULL OR verified_type = declared_type OR status <> 'CONNECTED'),
      CHECK ((verified_type IS NULL) = (verified_at IS NULL))
    );
    -- One live record per MT5 account; a disconnected one may be added again (a wrong type).
    CREATE UNIQUE INDEX broker_connections_account ON broker_connections (login, server)
      WHERE status <> 'DISCONNECTED';
    CREATE UNIQUE INDEX broker_connections_one_per_slot ON broker_connections (slot)
      WHERE slot IS NOT NULL AND status <> 'DISCONNECTED';
    REVOKE DELETE, TRUNCATE ON broker_connections FROM {app};

    CREATE TABLE broker_credentials (
      id uuid PRIMARY KEY,
      connection_id uuid NOT NULL REFERENCES broker_connections (id),
      kind text NOT NULL CHECK (kind IN ('MASTER','INVESTOR')),
      ciphertext bytea NOT NULL,
      nonce bytea NOT NULL CHECK (length(nonce) = 12),
      added_at timestamptz NOT NULL,
      added_by text NOT NULL,
      revoked_at timestamptz,
      revoked_by text,
      CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
    );
    CREATE UNIQUE INDEX broker_credentials_one_active ON broker_credentials (connection_id)
      WHERE revoked_at IS NULL;
    CREATE FUNCTION broker_credentials_sealed() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.id <> OLD.id OR NEW.connection_id <> OLD.connection_id OR NEW.kind <> OLD.kind
         OR NEW.ciphertext <> OLD.ciphertext OR NEW.nonce <> OLD.nonce
         OR NEW.added_at <> OLD.added_at OR NEW.added_by <> OLD.added_by
         OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
        RAISE EXCEPTION 'a stored password is sealed: only its revocation may be recorded'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER broker_credentials_sealed BEFORE UPDATE ON broker_credentials
      FOR EACH ROW EXECUTE FUNCTION broker_credentials_sealed();
    REVOKE DELETE, TRUNCATE ON broker_credentials FROM {app};

    CREATE TABLE broker_connection_events (
      id uuid PRIMARY KEY,
      connection_id uuid REFERENCES broker_connections (id),
      at timestamptz NOT NULL,
      kind text NOT NULL CHECK (kind IN ('ADDED','EDITED','PASSWORD','TEST','SAVED',
        'DISCONNECTED','RECONNECTED','SIGN_IN','HEALTH','ASSIGNED','REFUSED','EXECUTION')),
      ok boolean NOT NULL,
      code text,
      message text NOT NULL,
      detail jsonb,
      actor text NOT NULL
    );
    CREATE INDEX broker_connection_events_latest ON broker_connection_events (connection_id, at);
    CREATE TRIGGER broker_connection_events_immutable
      BEFORE UPDATE OR DELETE ON broker_connection_events
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON broker_connection_events FROM {app};

    CREATE TABLE robot_assignments (
      robot text PRIMARY KEY CHECK (robot IN ('DEMO_ROBOT','LIVE_ROBOT')),
      connection_id uuid REFERENCES broker_connections (id),
      assigned_at timestamptz NOT NULL,
      assigned_by text NOT NULL
    );
    REVOKE DELETE, TRUNCATE ON robot_assignments FROM {app};

    -- Bridge 7's commands about the account itself: sign in, and who is signed in.
    ALTER TABLE bridge_commands DROP CONSTRAINT bridge_commands_kind_check;
    ALTER TABLE bridge_commands ADD CONSTRAINT bridge_commands_kind_check
      CHECK (kind IN ('SUBMIT','CANCEL','PING','RATES','LOGIN','VERIFY'));
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM broker_connections) OR EXISTS (SELECT 1 FROM robot_assignments)
         OR EXISTS (SELECT 1 FROM broker_connection_events)
         OR EXISTS (SELECT 1 FROM bridge_commands WHERE kind IN ('LOGIN','VERIFY')) THEN
        RAISE EXCEPTION 'broker connections exist; the schema before 0030 cannot hold them';
      END IF;
    END $$;
    ALTER TABLE bridge_commands DROP CONSTRAINT bridge_commands_kind_check;
    ALTER TABLE bridge_commands ADD CONSTRAINT bridge_commands_kind_check
      CHECK (kind IN ('SUBMIT','CANCEL','PING','RATES'));
    DROP TABLE robot_assignments;
    DROP TABLE broker_connection_events;
    DROP TABLE broker_credentials;
    DROP FUNCTION broker_credentials_sealed();
    DROP TABLE broker_connections;
    """
    )
