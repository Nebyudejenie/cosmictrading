"""AI provider keys entered by the owner in the portal (S81, the owner's choice of 2026-11-11).

The owner chose to type AI provider API keys in Admin → AI Configuration rather than have
them installed as server secret files. A key is stored only encrypted (AES-GCM, a key derived
from the server's own secret, the provider bound in as associated data so it cannot be used for
another provider), with its last four characters for the mask; it is decrypted only by the
worker, which calls the provider. Free AI only stays (FREE_CLOUD_AI_ONLY, $0).

- ``ai_provider_keys``: at most one active key per provider; replaced or revoked, never deleted.
- ``ai_provider_tests``: append-only results of the owner's connection and inference tests.

The downgrade refuses while either table holds a row.

Revision ID: 0029_ai_provider_keys
Revises: 0028_robot_controls
Create Date: 2026-11-11
"""

from cosmic.db.sqlsplit import run_script

revision = "0029_ai_provider_keys"
down_revision = "0028_robot_controls"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        f"""
    CREATE TABLE ai_provider_keys (
      id uuid PRIMARY KEY,
      provider_key text NOT NULL,
      ciphertext bytea NOT NULL,
      nonce bytea NOT NULL CHECK (length(nonce) = 12),
      last4 text NOT NULL CHECK (length(last4) BETWEEN 1 AND 4),
      added_at timestamptz NOT NULL,
      added_by text NOT NULL,
      revoked_at timestamptz,
      revoked_by text,
      CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
    );
    CREATE UNIQUE INDEX ai_provider_keys_one_active ON ai_provider_keys (provider_key)
      WHERE revoked_at IS NULL;
    REVOKE DELETE, TRUNCATE ON ai_provider_keys FROM {app};

    CREATE TABLE ai_provider_tests (
      id uuid PRIMARY KEY,
      provider_key text NOT NULL,
      model_id text,
      at timestamptz NOT NULL,
      key_source text NOT NULL CHECK (key_source IN ('PORTAL','FILE','NONE')),
      auth_ok boolean NOT NULL,
      model_ok boolean NOT NULL,
      inference_ok boolean NOT NULL,
      schema_ok boolean NOT NULL,
      latency_ms integer,
      prompt_tokens integer,
      completion_tokens integer,
      detail text NOT NULL,
      answer jsonb
    );
    CREATE INDEX ai_provider_tests_latest ON ai_provider_tests (provider_key, at);
    CREATE TRIGGER ai_provider_tests_immutable BEFORE UPDATE OR DELETE ON ai_provider_tests
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON ai_provider_tests FROM {app};
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM ai_provider_keys) OR EXISTS (SELECT 1 FROM ai_provider_tests) THEN
        RAISE EXCEPTION 'AI keys or tests exist; the schema before 0029 cannot hold them';
      END IF;
    END $$;
    DROP TABLE ai_provider_tests;
    DROP TABLE ai_provider_keys;
    """
    )
