"""A real-money account names its key (ADR-034, S40c).

- ``accounts.key_fingerprint``: the fingerprint of the venue key a LIVE account was opened
  with (16 hex characters, never the key). Required for a LIVE account and fixed once set, so
  lock 5 (the key check) can count only a check of that very key.
- A LIVE account is only ever inserted as one: no account becomes LIVE, or stops being LIVE,
  by an update (its simulated history must never turn into real money).
- At most one ACTIVE LIVE account per adapter: the live link speaks for one venue account.

The downgrade refuses while a LIVE account exists (nothing is deleted).

Revision ID: 0017_live_account_key
Revises: 0016_live_locks
Create Date: 2026-10-30
"""

from cosmic.db.sqlsplit import run_script

revision = "0017_live_account_key"
down_revision = "0016_live_locks"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    ALTER TABLE accounts ADD COLUMN key_fingerprint text;
    ALTER TABLE accounts ADD CONSTRAINT accounts_live_key_check CHECK (
      (kind <> 'LIVE' OR key_fingerprint IS NOT NULL)
      AND (key_fingerprint IS NULL OR key_fingerprint ~ '^[0-9a-f]{16}$')
    );
    CREATE UNIQUE INDEX accounts_one_active_live ON accounts (broker_adapter)
      WHERE kind = 'LIVE' AND status = 'ACTIVE';

    CREATE FUNCTION accounts_key_fixed() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.key_fingerprint IS NOT NULL
         AND NEW.key_fingerprint IS DISTINCT FROM OLD.key_fingerprint THEN
        RAISE EXCEPTION 'an account''s key never changes; open a new account for a new key'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      IF OLD.kind = 'LIVE' AND NEW.kind IS DISTINCT FROM OLD.kind THEN
        RAISE EXCEPTION 'a real-money account stays a real-money account'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      IF NEW.kind = 'LIVE' AND OLD.kind IS DISTINCT FROM 'LIVE' THEN
        RAISE EXCEPTION 'an account becomes a real-money account only by being opened as one'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER accounts_key_fixed BEFORE UPDATE OF key_fingerprint, kind ON accounts
      FOR EACH ROW EXECUTE FUNCTION accounts_key_fixed();
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script("""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM accounts WHERE kind = 'LIVE') THEN
        RAISE EXCEPTION 'LIVE accounts exist; the schema before 0017 cannot hold their keys';
      END IF;
    END $$;
    DROP TRIGGER accounts_key_fixed ON accounts;
    DROP FUNCTION accounts_key_fixed();
    DROP INDEX accounts_one_active_live;
    ALTER TABLE accounts DROP CONSTRAINT accounts_live_key_check;
    ALTER TABLE accounts DROP COLUMN key_fingerprint;
    """)
