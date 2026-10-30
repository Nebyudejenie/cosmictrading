"""The real-money locks in the database (ADR-034): nothing reaches real money without the
owner's live activation, whatever the application does.

- ``live_activations`` (unused since 0003) gets its terms: the one LIVE account it covers, a
  money cap, an expiry within 90 days and a written reason. Rows are immutable; a revocation
  is its own append-only row in ``live_activation_revocations``, so what was allowed is never
  rewritten. An activation is only for a LIVE account, by an active owner.
- ``live_key_checks``: append-only results of reading a live key's own permissions from the
  venue (booleans, failures and a fingerprint; never the key).
- ``accounts``: a LIVE account only with the adapter ``binance_spot_live``.
- ``orders_live_guard``: an order on a LIVE account needs an activation of that account active
  at the order's time (and not expired by the database's clock), and its value within the cap.
  Only a reduce-only SELL exit or stop goes without one: on spot every buy adds exposure. A
  real-money order's account, purpose, side, size and price never change.
- ``trading_mandates_guard`` (extended, not weakened): a mandate on a LIVE account only in
  ASK mode, under an active activation, with its money within the cap and its expiry no later
  than the activation's. ``approvals_mandate_guard`` is unchanged: no MANDATE approval on a
  LIVE account.

The upgrade refuses if ``live_activations`` has rows (it was never written). The downgrade
refuses while any activation, key check or LIVE account exists (nothing is deleted).

Revision ID: 0016_live_locks
Revises: 0015_mandate_timeframe
Create Date: 2026-10-30
"""

from cosmic.db.sqlsplit import run_script

revision = "0016_live_locks"
down_revision = "0015_mandate_timeframe"
branch_labels = None
depends_on = None

MANDATE_GUARD_0011 = """
    CREATE OR REPLACE FUNCTION trading_mandates_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM accounts WHERE id = NEW.account_id AND kind IN ('PAPER','SANDBOX')
      ) THEN
        RAISE EXCEPTION 'a mandate is allowed only on a PAPER or SANDBOX account'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      IF TG_OP = 'UPDATE' THEN
        IF OLD.status <> 'ACTIVE' THEN
          RAISE EXCEPTION 'an ended mandate stays ended' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF (to_jsonb(NEW) - 'status' - 'ended_at' - 'ended_by' - 'end_reason')
           IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'ended_at' - 'ended_by' - 'end_reason')
        THEN
          RAISE EXCEPTION 'mandate terms are immutable; grant a new mandate'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
      END IF;
      RETURN NEW;
    END $$;
"""


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(f"""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM live_activations) THEN
        RAISE EXCEPTION 'live_activations has rows; 0016 expects the unused table from 0003';
      END IF;
    END $$;
    ALTER TABLE live_activations DROP COLUMN revoked_at;
    ALTER TABLE live_activations
      ADD COLUMN account_id uuid NOT NULL REFERENCES accounts(id),
      ADD COLUMN cap numeric(38,18) NOT NULL CHECK (cap > 0),
      ADD COLUMN expires_at timestamptz NOT NULL,
      ADD COLUMN reason text NOT NULL CHECK (length(btrim(reason)) >= 5),
      ADD CONSTRAINT live_activations_expiry_check
        CHECK (expires_at > activated_at AND expires_at <= activated_at + interval '90 days');
    CREATE INDEX live_activations_account ON live_activations (account_id, activated_at);
    CREATE TRIGGER live_activations_immutable BEFORE UPDATE OR DELETE ON live_activations
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON live_activations FROM {app};

    CREATE FUNCTION live_activations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM accounts WHERE id = NEW.account_id AND kind = 'LIVE') THEN
        RAISE EXCEPTION 'a live activation is only for a LIVE account'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM users WHERE id = NEW.activated_by AND is_active AND 'OWNER' = ANY(roles)
      ) THEN
        RAISE EXCEPTION 'only an active owner can activate real money'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER live_activations_guard BEFORE INSERT ON live_activations
      FOR EACH ROW EXECUTE FUNCTION live_activations_guard();

    CREATE TABLE live_activation_revocations (
      activation_id uuid PRIMARY KEY REFERENCES live_activations(id),
      revoked_at timestamptz NOT NULL,
      revoked_by text NOT NULL,
      reason text NOT NULL CHECK (length(btrim(reason)) >= 3)
    );
    CREATE TRIGGER live_activation_revocations_immutable
      BEFORE UPDATE OR DELETE ON live_activation_revocations
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON live_activation_revocations FROM {app};

    CREATE TABLE live_key_checks (
      id uuid PRIMARY KEY,
      adapter text NOT NULL,
      key_fingerprint text NOT NULL,
      passed boolean NOT NULL,
      restrictions jsonb NOT NULL,
      failures jsonb NOT NULL,
      checked_at timestamptz NOT NULL,
      actor_type text NOT NULL,
      actor_id text NOT NULL,
      CHECK (passed = (jsonb_array_length(failures) = 0))
    );
    CREATE INDEX live_key_checks_latest ON live_key_checks (adapter, checked_at);
    CREATE TRIGGER live_key_checks_immutable BEFORE UPDATE OR DELETE ON live_key_checks
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON live_key_checks FROM {app};

    ALTER TABLE accounts DROP CONSTRAINT accounts_adapter_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_adapter_check CHECK (
      (kind = 'PAPER' AND broker_adapter = 'paper')
      OR (kind = 'SANDBOX' AND broker_adapter IN ('binance_spot_testnet','hfm_mt5_demo'))
      OR (kind = 'LIVE' AND broker_adapter = 'binance_spot_live')
    );

    -- The account's activation in force at a moment: unexpired and never revoked.
    CREATE FUNCTION live_activation_at(account uuid, at timestamptz)
      RETURNS TABLE (id uuid, cap numeric, expires_at timestamptz)
      LANGUAGE sql STABLE AS $$
      SELECT a.id, a.cap, a.expires_at FROM live_activations a
      WHERE a.account_id = account AND a.activated_at <= at AND a.expires_at > at
        AND NOT EXISTS (
          SELECT 1 FROM live_activation_revocations r WHERE r.activation_id = a.id
        )
      ORDER BY a.activated_at DESC LIMIT 1
    $$;

    -- The database's clock bounds the application's: an order or mandate stamped in the past
    -- cannot reach back into an activation that has since expired.
    CREATE FUNCTION orders_live_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      allowed numeric;
    BEGIN
      IF TG_OP = 'UPDATE' THEN
        IF EXISTS (
          SELECT 1 FROM accounts WHERE id IN (OLD.account_id, NEW.account_id) AND kind = 'LIVE'
        ) THEN
          RAISE EXCEPTION 'a real-money order''s account, purpose, side, size and price never change'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW;
      END IF;
      IF NOT EXISTS (SELECT 1 FROM accounts WHERE id = NEW.account_id AND kind = 'LIVE') THEN
        RETURN NEW;
      END IF;
      -- Only a sell that closes (an exit or a stop) goes without an activation: on spot every
      -- buy adds exposure, whatever its flags say.
      IF NEW.reduce_only AND NEW.purpose <> 'ENTRY' AND NEW.side = 'SELL' THEN
        RETURN NEW;
      END IF;
      SELECT l.cap INTO allowed
        FROM live_activation_at(NEW.account_id, GREATEST(NEW.created_at, statement_timestamp())) l;
      IF allowed IS NULL THEN
        RAISE EXCEPTION 'a real-money order needs the owner''s live activation of this account'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      IF NEW.quantity * NEW.limit_price > allowed THEN
        RAISE EXCEPTION 'a real-money order above the live activation''s cap (%)', allowed
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER orders_live_guard
      BEFORE INSERT OR UPDATE OF account_id, purpose, side, quantity, limit_price, reduce_only
      ON orders FOR EACH ROW EXECUTE FUNCTION orders_live_guard();

    CREATE OR REPLACE FUNCTION trading_mandates_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      kind_now text;
      act_id uuid;
      act_cap numeric;
      act_expires timestamptz;
    BEGIN
      SELECT kind INTO kind_now FROM accounts WHERE id = NEW.account_id;
      IF kind_now IS NULL OR kind_now NOT IN ('PAPER','SANDBOX','LIVE') THEN
        RAISE EXCEPTION 'a mandate is allowed only on a PAPER or SANDBOX account'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      IF kind_now = 'LIVE' AND TG_OP = 'INSERT' THEN
        SELECT l.id, l.cap, l.expires_at INTO act_id, act_cap, act_expires
          FROM live_activation_at(NEW.account_id, GREATEST(NEW.granted_at, statement_timestamp())) l;
        IF NEW.mode <> 'ASK' OR act_id IS NULL OR NEW.capital IS NULL
           OR NEW.capital > act_cap OR NEW.expires_at > act_expires THEN
          RAISE EXCEPTION 'a mandate on a real-money account must ask before each trade, within the owner''s live activation (money within its cap, ending by its expiry)'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
      END IF;
      IF TG_OP = 'UPDATE' THEN
        IF OLD.status <> 'ACTIVE' THEN
          RAISE EXCEPTION 'an ended mandate stays ended' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF (to_jsonb(NEW) - 'status' - 'ended_at' - 'ended_by' - 'end_reason')
           IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'ended_at' - 'ended_by' - 'end_reason')
        THEN
          RAISE EXCEPTION 'mandate terms are immutable; grant a new mandate'
            USING ERRCODE = 'insufficient_privilege';
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    """)


def downgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM live_activations) OR EXISTS (SELECT 1 FROM live_key_checks)
         OR EXISTS (SELECT 1 FROM accounts WHERE kind = 'LIVE') THEN
        RAISE EXCEPTION
          'live activations, key checks or LIVE accounts exist; the schema before 0016 cannot hold them';
      END IF;
    END $$;
    """
        + MANDATE_GUARD_0011
        + f"""
    DROP TRIGGER orders_live_guard ON orders;
    DROP FUNCTION orders_live_guard();
    DROP FUNCTION live_activation_at(uuid, timestamptz);
    ALTER TABLE accounts DROP CONSTRAINT accounts_adapter_check;
    ALTER TABLE accounts ADD CONSTRAINT accounts_adapter_check CHECK (
      (kind = 'PAPER' AND broker_adapter = 'paper')
      OR (kind = 'SANDBOX' AND broker_adapter IN ('binance_spot_testnet','hfm_mt5_demo'))
      OR kind = 'LIVE'
    );
    DROP TABLE live_key_checks;
    DROP TABLE live_activation_revocations;
    DROP TRIGGER live_activations_guard ON live_activations;
    DROP FUNCTION live_activations_guard();
    DROP TRIGGER live_activations_immutable ON live_activations;
    GRANT UPDATE, DELETE ON live_activations TO {app};
    DROP INDEX live_activations_account;
    ALTER TABLE live_activations
      DROP CONSTRAINT live_activations_expiry_check,
      DROP COLUMN reason,
      DROP COLUMN expires_at,
      DROP COLUMN cap,
      DROP COLUMN account_id;
    ALTER TABLE live_activations ADD COLUMN revoked_at timestamptz;
    """
    )
