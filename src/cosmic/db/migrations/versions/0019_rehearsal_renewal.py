"""A $100 rehearsal that renews itself (S49, ADR-035 addendum).

- ``trading_mandates.auto_renew``: the owner chose, when granting the run with a code, that it
  renews itself on the same terms when it ends. Only on a simulated (PAPER) account.
- ``trading_mandates.renews``: the run an automatic renewal continues. The guard checks that it
  keeps the owner's terms (the same account, rule, timeframe, markets, mode, care level, trade
  counts and owner; a loss per trade within the care level and the same daily stop for it) and
  never lowers the floor (its money less its total loss stop: equal, or higher when an open
  trade's worst case is kept apart), that it continues the account's latest run, one that
  expired (never one stopped, revoked or still running) and whose renewal the owner has not
  stopped, that it lasts no longer than that run, and that a run is continued at most once.
- ``mandate_renewal_stops``: the owner's "stop renewing" for a run; append-only.

The downgrade refuses while any of them is used, and restores 0016's guard.

Revision ID: 0019_rehearsal_renewal
Revises: 0018_fill_book_checks
Create Date: 2026-11-02
"""

from cosmic.db.sqlsplit import run_script

revision = "0019_rehearsal_renewal"
down_revision = "0018_fill_book_checks"
branch_labels = None
depends_on = None

# 0016's guard, as it was: restored by the downgrade.
GUARD_0016 = """
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
"""

GUARD_0019 = """
    CREATE OR REPLACE FUNCTION trading_mandates_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      kind_now text;
      act_id uuid;
      act_cap numeric;
      act_expires timestamptz;
      prev trading_mandates%ROWTYPE;
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
      IF TG_OP = 'INSERT' AND NEW.auto_renew AND kind_now <> 'PAPER' THEN
        RAISE EXCEPTION 'only an auto-trader on simulated (paper) money may renew itself'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      IF TG_OP = 'INSERT' AND NEW.renews IS NOT NULL THEN
        SELECT * INTO prev FROM trading_mandates WHERE id = NEW.renews;
        IF NOT FOUND OR NOT prev.auto_renew OR NOT NEW.auto_renew OR prev.status <> 'EXPIRED'
           OR prev.account_id <> NEW.account_id OR prev.strategy_key <> NEW.strategy_key
           OR prev.timeframe <> NEW.timeframe OR prev.instruments <> NEW.instruments
           OR prev.mode <> NEW.mode OR prev.risk_pct IS DISTINCT FROM NEW.risk_pct
           OR prev.max_trades_per_day <> NEW.max_trades_per_day
           OR prev.max_open_positions <> NEW.max_open_positions
           OR prev.granted_by <> NEW.granted_by
           OR prev.capital IS NULL OR NEW.capital IS NULL
           OR prev.total_loss_limit IS NULL OR NEW.total_loss_limit IS NULL
           OR NEW.capital - NEW.total_loss_limit < prev.capital - prev.total_loss_limit
           OR NEW.risk_pct IS NULL
           OR NEW.max_loss_per_trade > NEW.capital * NEW.risk_pct / 100
           OR NEW.daily_loss_limit * prev.max_loss_per_trade
              > prev.daily_loss_limit * NEW.max_loss_per_trade
           OR NEW.expires_at - NEW.granted_at > prev.expires_at - prev.granted_at
           OR EXISTS (SELECT 1 FROM mandate_renewal_stops s WHERE s.mandate_id = prev.id)
           OR EXISTS (SELECT 1 FROM trading_mandates t
                      WHERE t.account_id = NEW.account_id AND t.granted_at > prev.granted_at)
        THEN
          RAISE EXCEPTION 'an automatic renewal keeps the owner''s terms and care, never lowers the floor, and continues only the latest run, which expired with renewal chosen and not stopped'
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
"""


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        f"""
    ALTER TABLE trading_mandates ADD COLUMN auto_renew boolean NOT NULL DEFAULT false;
    ALTER TABLE trading_mandates ADD COLUMN renews uuid REFERENCES trading_mandates(id);
    CREATE UNIQUE INDEX trading_mandates_renews ON trading_mandates (renews)
      WHERE renews IS NOT NULL;
    CREATE TABLE mandate_renewal_stops (
      mandate_id uuid PRIMARY KEY REFERENCES trading_mandates(id),
      stopped_by uuid NOT NULL REFERENCES users(id),
      stopped_at timestamptz NOT NULL,
      reason text NOT NULL
    );
    CREATE TRIGGER mandate_renewal_stops_immutable BEFORE UPDATE OR DELETE
      ON mandate_renewal_stops FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON mandate_renewal_stops FROM {app};
    """
        + GUARD_0019
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM trading_mandates WHERE auto_renew OR renews IS NOT NULL)
         OR EXISTS (SELECT 1 FROM mandate_renewal_stops) THEN
        RAISE EXCEPTION 'self-renewing runs exist; the schema before 0019 cannot hold them';
      END IF;
    END $$;
    """
        + GUARD_0016
        + """
    DROP TABLE mandate_renewal_stops;
    DROP INDEX trading_mandates_renews;
    ALTER TABLE trading_mandates DROP COLUMN renews;
    ALTER TABLE trading_mandates DROP COLUMN auto_renew;
    """
    )
